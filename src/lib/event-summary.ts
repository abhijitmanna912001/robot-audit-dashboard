// One compact summary line per audit event, for the timeline. Pure and display only.
// Built from the defensive readers in payload.ts, so payloads stay untrusted: a missing or
// wrong-typed field is left out, nothing is invented, and nothing throws. Returns undefined
// when there is nothing informative to show.
import {
  asRecord,
  readCritic,
  readExecution,
  readPlan,
  readPlanFailure,
  readSource,
  readValidator,
  text,
} from "./payload.ts";

export const MAX_LINE = 120;
const MAX_PIECE = 40;
const MAX_ACTIONS = 3;
const MAX_RULES = 3;

type EventLike = { eventType: unknown; payload: unknown };

// What the timeline shows: `text` is plain, `hash` is the full hash (for a copy button) and
// `shortHash` is its display form. Either part may be missing, never both.
export type EventSummary = { text: string; hash?: string; shortHash?: string };

// actionId -> target, taken from the same command's plan_generated events.
export type ActionTargets = ReadonlyMap<string, string>;
export type PlanInfo = { planId?: string; actionCount: number };
export type SummaryContext = { targets: ActionTargets; plans: readonly PlanInfo[] };

function clip(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

const piece = (v: unknown): string | undefined => {
  const t = text(v, MAX_PIECE);
  return t === undefined ? undefined : t.replace(/\s+/g, " ").trim();
};

function join(parts: (string | undefined)[]): string {
  const out = parts.filter((p): p is string => p !== undefined && p !== "").join(" · ");
  return clip(out, MAX_LINE);
}

// "sha256:ab12..." -> "ab12cd34…". Needs 8 or more hex characters after an optional prefix.
export function shortHash(h: unknown): string | undefined {
  if (typeof h !== "string") return undefined;
  const rest = h.trim().replace(/^sha256:/i, "");
  return /^[0-9a-f]{8,}$/i.test(rest) ? `${rest.slice(0, 8).toLowerCase()}…` : undefined;
}

function withHash(textPart: string, hash: string | undefined): EventSummary | undefined {
  const short = shortHash(hash);
  if (textPart === "" && short === undefined) return undefined;
  return short === undefined ? { text: textPart } : { text: textPart, hash, shortHash: short };
}

// Builds the lookups from every plan_generated event. A later duplicate actionId is ignored.
export function eventContext(events: readonly EventLike[]): SummaryContext {
  const targets = new Map<string, string>();
  const plans: PlanInfo[] = [];
  for (const e of events) {
    if (e?.eventType !== "plan_generated" || readPlanFailure(e.payload)) continue;
    const p = readPlan(e.payload);
    plans.push({ planId: p.planId, actionCount: p.actions.length + p.extraActions });
    for (const a of p.actions) {
      if (a.actionId !== undefined && a.target !== undefined && !targets.has(a.actionId)) {
        targets.set(a.actionId, a.target);
      }
    }
  }
  return { targets, plans };
}

export function actionTargets(events: readonly EventLike[]): ActionTargets {
  return eventContext(events).targets;
}

function moreSuffix(shown: number, total: number): string {
  return total > shown ? ` +${total - shown} more` : "";
}

function planSummary(payload: unknown): EventSummary | undefined {
  const failure = readPlanFailure(payload);
  if (failure) return { text: join(["planner error", piece(failure.kind)]) };
  const plan = readPlan(payload);
  const parts = plan.actions.slice(0, MAX_ACTIONS).flatMap((a) => {
    const tail = [a.value === undefined ? undefined : `${a.value}`, piece(a.unit)]
      .filter((x) => x !== undefined)
      .join(" ");
    const target = piece(a.target);
    const one = target && tail ? `${target} -> ${tail}` : (target ?? (tail || undefined));
    return one === undefined ? [] : [one];
  });
  const total = plan.actions.length + plan.extraActions;
  const list = parts.length === 0 ? "" : clip(`${parts.join(", ")}${moreSuffix(MAX_ACTIONS, total)}`, MAX_LINE);
  return withHash(list, plan.planHash);
}

function validatorSummary(payload: unknown, ctx?: SummaryContext): EventSummary | undefined {
  const v = readValidator(payload);
  if (v.outcome === "pass") {
    const planId = text(asRecord(payload)?.planId);
    const plans = ctx?.plans ?? [];
    const plan = (planId !== undefined ? plans.find((p) => p.planId === planId) : undefined) ?? plans[plans.length - 1];
    const version = piece(v.validatorVersion);
    const line = join([
      plan ? `${plan.actionCount} action(s) checked` : undefined,
      version ? `validator v${version}` : undefined,
    ]);
    return line === "" ? undefined : { text: line };
  }
  if (v.outcome === "fail") {
    const withRule = v.errors.filter((x) => x.ruleId !== undefined);
    const shown = withRule.slice(0, MAX_RULES).map((x) => {
      const joint = typeof x.actionId === "string" ? piece(ctx?.targets.get(x.actionId)) : undefined;
      return joint ? `${piece(x.ruleId)} on ${joint}` : (piece(x.ruleId) as string);
    });
    if (shown.length === 0) return undefined;
    return { text: clip(`${shown.join(", ")}${moreSuffix(MAX_RULES, withRule.length + v.extraErrors)}`, MAX_LINE) };
  }
  return undefined;
}

function criticSummary(payload: unknown): EventSummary | undefined {
  const c = readCritic(payload);
  if (c.outcome === "approve") {
    const reported = Array.isArray(asRecord(asRecord(payload)?.response)?.triggeredRules);
    const n = c.triggeredRules.length + c.extraRules;
    const count = reported ? `${n} ${n === 1 ? "rule" : "rules"} triggered` : "";
    return withHash(count, c.planHash ?? c.responsePlanHash);
  }
  if (c.outcome === "reject") {
    if (c.triggeredRules.length === 0) return undefined;
    const shown = c.triggeredRules.slice(0, MAX_RULES).map((r) => piece(r) as string);
    return { text: clip(`${shown.join(", ")}${moreSuffix(MAX_RULES, c.triggeredRules.length + c.extraRules)}`, MAX_LINE) };
  }
  if (c.outcome === "error") {
    const kind = piece(c.error?.kind);
    return kind ? { text: kind } : undefined;
  }
  return undefined;
}

export function summarizeEvent(e: EventLike, ctx?: SummaryContext): EventSummary | undefined {
  if (typeof e !== "object" || e === null) return undefined;
  switch (e.eventType) {
    case "plan_generated":
      return planSummary(e.payload);
    case "validator_result":
      return validatorSummary(e.payload, ctx);
    case "critic_result":
      return criticSummary(e.payload);
    case "execution_started":
    case "execution_completed":
    case "execution_failed": {
      const { dryRun } = readExecution(e.payload);
      return dryRun === undefined ? undefined : { text: dryRun ? "dry run" : "executed" };
    }
    case "command_received": {
      const source = piece(readSource(e.payload));
      return source === undefined ? undefined : { text: source };
    }
    default:
      return undefined;
  }
}

// True when the summary only repeats the title (ignoring case and punctuation), so the
// timeline can hide the second line. A summary word counts as repeated when the title has the
// same word or a close stem of it ("approve" / "approved", "executed" / "execution").
const words = (v: string) => v.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim().split(" ").filter(Boolean);

function sameStem(a: string, b: string): boolean {
  if (a === b) return true;
  const [short, long] = a.length <= b.length ? [a, b] : [b, a];
  if (short.length >= 4 && long.startsWith(short)) return true;
  let i = 0;
  while (i < short.length && short[i] === long[i]) i += 1;
  return i >= 5;
}

export function isRedundant(title: unknown, summary: unknown): boolean {
  if (typeof title !== "string" || typeof summary !== "string") return false;
  const t = words(title);
  const s = words(summary);
  return s.length > 0 && t.length > 0 && s.every((w) => t.some((x) => sameStem(w, x)));
}
