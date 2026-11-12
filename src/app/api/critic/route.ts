// Deterministic MOCK critic. Fails closed: anything unexpected is a reject.
// Uses DRAFT types; no OpenAI, no Supabase yet.
import { timingSafeEqual } from "node:crypto";
import { computePlanHash } from "../../../lib/plan-hash.ts";
import type { CriticResponse } from "@/types/draft";

const JOINT_LIMIT = 1.57; // radians
const MAX_FORWARD = 2; // metres
const KNOWN_TYPES = new Set(["set_joint", "move_forward", "stop"]);

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

    body.actions.forEach((a: unknown, i: number) => {
      if (containsNull(a)) {
        fail("null_value_not_allowed", `Action #${i}: null values are not allowed; omit unset fields.`);
        return;
      }
      if (
        !isObject(a) ||
        !nonEmptyString(a.actionId) ||
        !nonEmptyString(a.reason) ||
        (a.value !== undefined && !(typeof a.value === "number" && Number.isFinite(a.value)))
      ) {
        fail("malformed_action", `Action #${i} is malformed.`);
        return;
      }
      const label = `Action ${a.actionId}`;
      if (typeof a.type !== "string" || !KNOWN_TYPES.has(a.type)) {
        fail("unknown_action_type", `${label}: unknown action type.`);
        return;
      }
      if (a.type === "set_joint") {
        if (a.value === undefined || Math.abs(a.value as number) > JOINT_LIMIT) {
          fail("joint_out_of_range", `${label}: set_joint value must be within -${JOINT_LIMIT} to ${JOINT_LIMIT} rad.`);
        }
      } else if (a.type === "move_forward") {
        if (a.value === undefined || (a.value as number) > MAX_FORWARD) {
          fail("move_forward_too_far", `${label}: move_forward value must be at most ${MAX_FORWARD} m.`);
        } else if ((a.value as number) < 0) {
          fail("move_forward_negative", `${label}: move_forward value must not be negative.`);
        }
      }
    });

    if (rules.size > 0) {
      return reject(commandId, planId, reasons.join(" "), [...rules], 200, planHash);
    }

    const approved: CriticResponse = {
      commandId,
      planId,
      planHash,
      verdict: "approve",
      reason: "All actions passed all mock safety rules.",
      triggeredRules: [],
    };
    return Response.json(approved);
  } catch {
    return reject(commandId, planId, "Critic failed unexpectedly; failing closed.", ["critic_error"], 500);
  }
}
