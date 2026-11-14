// One compact summary line per audit event, for the timeline. Pure and display only.
// Built from the defensive readers in payload.ts, so payloads stay untrusted: a missing or
// wrong-typed field is left out, nothing is invented, and nothing throws. Returns undefined
// when there is nothing readable to show.
import { readCritic, readExecution, readPlan, readPlanFailure, readSource, readValidator, text } from "./payload.ts";

export const MAX_LINE = 120;
const MAX_PIECE = 40;
const MAX_ACTIONS = 3;

type EventLike = { eventType: unknown; payload: unknown };

// actionId -> target, taken from the same command's plan_generated event.
export type ActionTargets = ReadonlyMap<string, string>;

function clip(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

const piece = (v: unknown): string | undefined => {
  const t = text(v, MAX_PIECE);
  return t === undefined ? undefined : t.replace(/\s+/g, " ").trim();
};

function join(parts: (string | undefined)[]): string | undefined {
  const out = parts.filter((p): p is string => p !== undefined && p !== "").join(" · ");
  return out === "" ? undefined : clip(out, MAX_LINE);
}

// Builds the lookup from every plan_generated event. A later duplicate actionId is ignored.
export function actionTargets(events: EventLike[]): ActionTargets {
  const map = new Map<string, string>();
  for (const e of events) {
    if (e?.eventType !== "plan_generated") continue;
    for (const a of readPlan(e.payload).actions) {
      if (a.actionId !== undefined && a.target !== undefined && !map.has(a.actionId)) {
        map.set(a.actionId, a.target);
      }
    }
  }
  return map;
}

function planLine(payload: unknown): string | undefined {
  const failure = readPlanFailure(payload);
  if (failure) return join(["planner error", piece(failure.kind)]);
  const plan = readPlan(payload);
  const shown = plan.actions.slice(0, MAX_ACTIONS).map((a) => {
    const value = a.value === undefined ? undefined : `${a.value}`;
    const tail = [value, piece(a.unit)].filter((x) => x !== undefined).join(" ");
    const target = piece(a.target);
    if (target && tail) return `${target} -> ${tail}`;
    return target ?? (tail || undefined);
  });
  const parts = shown.filter((s): s is string => s !== undefined);
  const more = Math.max(0, plan.actions.length - MAX_ACTIONS) + plan.extraActions;
  if (parts.length === 0) return undefined;
  return clip(`${parts.join(", ")}${more > 0 ? ` +${more} more` : ""}`, MAX_LINE);
}

function validatorLine(payload: unknown, targets?: ActionTargets): string | undefined {
  const v = readValidator(payload);
  const first = v.errors.find((x) => x.ruleId !== undefined);
  let rule: string | undefined;
  if (first) {
    const joint =
      typeof first.actionId === "string" ? targets?.get(first.actionId) : undefined;
    const j = piece(joint);
    rule = j ? `${piece(first.ruleId)} on ${j}` : piece(first.ruleId);
  }
  const version = piece(v.validatorVersion);
  return join([piece(v.outcome), version ? `v${version}` : undefined, rule]);
}

function criticLine(payload: unknown): string | undefined {
  const c = readCritic(payload);
  return join([piece(c.outcome), piece(c.triggeredRules[0]) ?? piece(c.error?.kind)]);
}

export function summarizeEvent(e: EventLike, targets?: ActionTargets): string | undefined {
  if (typeof e !== "object" || e === null) return undefined;
  switch (e.eventType) {
    case "plan_generated":
      return planLine(e.payload);
    case "validator_result":
      return validatorLine(e.payload, targets);
    case "critic_result":
      return criticLine(e.payload);
    case "execution_started":
    case "execution_completed":
    case "execution_failed": {
      const { dryRun } = readExecution(e.payload);
      return dryRun === undefined ? undefined : dryRun ? "dry run" : "executed";
    }
    case "command_received":
      return piece(readSource(e.payload));
    default:
      return undefined;
  }
}
