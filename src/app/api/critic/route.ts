// Deterministic critic (no OpenAI, no Supabase yet). Fails closed: anything
// unexpected is a reject. Rules mirror worker/safety_validator.py; joint limits
// and the action cap come from shared/so101-limits.json.
import { timingSafeEqual } from "node:crypto";
import { computePlanHash } from "../../../lib/plan-hash.ts";
import { JOINT_LIMITS, JOINT_UNIT, MAX_ACTIONS } from "../../../lib/so101-limits.ts";
import type { CriticResponse } from "@/types/draft";

const ACTION_TYPE = "set_joint"; // the only supported action type
const ALLOWED_FIELDS = new Set(["actionId", "type", "target", "value", "unit", "reason"]);

function reject(
  commandId: string,
  planId: string,
  reason: string,
  triggeredRules: string[],
  status = 200,
  planHash?: string, // only ever the critic's own recomputed hash
) {
  const body: CriticResponse = {
    commandId,
    planId,
    ...(planHash !== undefined && { planHash }),
    verdict: "reject",
    reason,
    triggeredRules,
  };
  return Response.json(body, { status });
}

// True if null appears anywhere in the value (iterative, no recursion limit).
function containsNull(root: unknown): boolean {
  const stack: unknown[] = [root];
  while (stack.length > 0) {
    const v = stack.pop();
    if (v === null) return true;
    if (Array.isArray(v)) stack.push(...v);
    else if (typeof v === "object") stack.push(...Object.values(v as object));
  }
  return false;
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function nonEmptyString(x: unknown): x is string {
  return typeof x === "string" && x.length > 0;
}

// A string that is not empty or whitespace-only.
function nonBlankString(x: unknown): x is string {
  return typeof x === "string" && x.trim().length > 0;
}

// Constant-time compare that never throws on length mismatch.
function secretMatches(provided: string, expected: string): boolean {
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) {
    timingSafeEqual(b, b); // keep timing roughly uniform
    return false;
  }
  return timingSafeEqual(a, b);
}

function unauthorized() {
  return reject("unknown", "unknown", "Unauthorized.", ["unauthorized"], 401);
}

export async function POST(request: Request) {
  let commandId = "unknown";
  let planId = "unknown";
  try {
    // Auth gate: fail closed, before the body is read.
    const expected = process.env.CRITIC_SHARED_SECRET;
    const provided = request.headers.get("x-critic-secret");
    if (!expected || !provided || !secretMatches(provided, expected)) {
      return unauthorized();
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return reject(commandId, planId, "Request body is not valid JSON.", ["invalid_json"], 400);
    }

    if (isObject(body)) {
      if (typeof body.commandId === "string") commandId = body.commandId;
      if (typeof body.planId === "string") planId = body.planId;
    }

    if (
      !isObject(body) ||
      !nonEmptyString(body.commandId) ||
      !nonEmptyString(body.planId) ||
      typeof body.planHash !== "string" ||
      !Array.isArray(body.actions)
    ) {
      return reject(
        commandId,
        planId,
        "Missing or invalid required fields: commandId, planId, planHash (string) and actions (array) are required.",
        ["missing_fields"],
        400,
      );
    }

    // Plan hash gate: runs before every other rule. Never trust the supplied hash.
    let planHash: string;
    try {
      planHash = computePlanHash(body.actions);
    } catch (e) {
      const detail = e instanceof Error ? e.message : "unknown error";
      return reject(
        commandId,
        planId,
        `Plan hash could not be computed (${detail}); failing closed.`,
        ["plan_hash_error"],
      );
    }
    if (planHash !== body.planHash) {
      return reject(
        commandId,
        planId,
        `Plan hash mismatch: supplied ${body.planHash}, critic computed ${planHash}.`,
        ["plan_hash_mismatch"],
        200,
        planHash,
      );
    }

    if (body.actions.length === 0) {
      return reject(commandId, planId, "Plan has no actions.", ["empty_actions"], 200, planHash);
    }

    const rules = new Set<string>();
    const reasons: string[] = [];
    const fail = (rule: string, msg: string) => {
      rules.add(rule);
      reasons.push(msg);
    };

    const actionIds = new Set<string>();
    const duplicateIds = new Set<string>();
    const joints = new Set<string>();
    const duplicateJoints = new Set<string>();

    body.actions.forEach((a: unknown, i: number) => {
      if (containsNull(a)) {
        fail("null_value_not_allowed", `Action #${i}: null values are not allowed; omit unset fields.`);
        return;
      }
      if (!isObject(a)) {
        fail("malformed_action", `Action #${i} is malformed: not an object.`);
        return;
      }

      for (const key of Object.keys(a)) {
        if (!ALLOWED_FIELDS.has(key)) {
          fail("unexpected_field", `Action #${i}: unexpected field "${key}".`);
        }
      }

      const id = nonBlankString(a.actionId) ? a.actionId : undefined;
      const label = id !== undefined ? `Action ${id}` : `Action #${i}`;
      if (id === undefined) fail("malformed_action", `${label}: actionId must be a non-blank string.`);
      else if (actionIds.has(id)) duplicateIds.add(id);
      else actionIds.add(id);
      if (!nonBlankString(a.reason)) fail("malformed_action", `${label}: reason must be a non-blank string.`);

      if (!nonBlankString(a.type)) {
        fail("malformed_action", `${label}: type must be a non-blank string.`);
        return;
      }
      if (a.type !== ACTION_TYPE) {
        fail("unsupported_action_type", `${label}: only ${ACTION_TYPE} is supported.`);
        return;
      }

      // target must be a known joint. Map lookup, so "constructor" is not a joint.
      let limit: { min: number; max: number } | undefined;
      let knownJoint: string | undefined;
      if (!nonBlankString(a.target)) {
        fail("malformed_action", `${label}: target must be a non-blank string.`);
      } else if (!JOINT_LIMITS.has(a.target)) {
        fail("unknown_joint", `${label}: unknown joint.`);
      } else {
        knownJoint = a.target;
        limit = JOINT_LIMITS.get(a.target);
        if (joints.has(a.target)) duplicateJoints.add(a.target);
        else joints.add(a.target);
      }

      const value =
        typeof a.value === "number" && Number.isFinite(a.value) ? a.value : undefined;
      if (value === undefined) fail("malformed_action", `${label}: value must be a finite number.`);

      let unitOk = false;
      if (!nonBlankString(a.unit)) {
        fail("malformed_action", `${label}: unit must be a non-blank string.`);
      } else if (a.unit !== JOINT_UNIT) {
        fail("unit_mismatch", `${label}: unit must be "${JOINT_UNIT}". No conversion is done.`);
      } else {
        unitOk = true;
      }

      // Only meaningful for a known joint, a real number and the right unit.
      if (limit && knownJoint !== undefined && value !== undefined && unitOk) {
        if (value < limit.min || value > limit.max) {
          fail(
            "joint_limit_exceeded",
            `${label}: value is outside the limits for ${knownJoint} (${limit.min} to ${limit.max} ${JOINT_UNIT}).`,
          );
        }
      }
    });

    if (body.actions.length > MAX_ACTIONS) {
      fail("max_actions_exceeded", `Plan has ${body.actions.length} actions; the maximum is ${MAX_ACTIONS}.`);
    }
    if (duplicateIds.size > 0) {
      fail("duplicate_action_id", "Each actionId may be used only once in a plan.");
    }
    if (duplicateJoints.size > 0) {
      fail("duplicate_joint_target", "Each joint may be targeted by only one action in a plan.");
    }

    if (rules.size > 0) {
      return reject(commandId, planId, reasons.join(" "), [...rules], 200, planHash);
    }

    const approved: CriticResponse = {
      commandId,
      planId,
      planHash,
      verdict: "approve",
      reason: "All actions passed all critic safety rules.",
      triggeredRules: [],
    };
    return Response.json(approved);
  } catch {
    return reject(commandId, planId, "Critic failed unexpectedly; failing closed.", ["critic_error"], 500);
  }
}
