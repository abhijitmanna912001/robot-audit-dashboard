// Defensive readers for audit_events.payload (docs/schema-draft.md "Event payloads").
// audit_events is readable with the anon key, so payloads are untrusted: every input
// is `unknown`, every output string is capped plain text, every list is capped, and a
// missing or wrong-typed field is simply left out. Nothing here throws.

export const MAX_STRING = 500;
export const MAX_LIST = 20;
export const MAX_RAW = 2000;

export type Rec = Record<string, unknown>;

export function asRecord(v: unknown): Rec | undefined {
  return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Rec) : undefined;
}

// Controls and invisible format characters (bidi overrides, zero width) become spaces.
// Replacement is one-to-one, so the length check below stays valid.
const INVISIBLE = /[\p{Cc}\p{Cf}]/gu;

function cap(s: string, max: number): string {
  const head = s.slice(0, max + 1).replace(INVISIBLE, " ");
  if (head.length <= max) return head;
  let cut = head.slice(0, max);
  const last = cut.charCodeAt(cut.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1); // do not split a surrogate pair
  return `${cut}…`;
}

// A non-empty string, capped. Anything else is undefined.
export function text(v: unknown, max = MAX_STRING): string | undefined {
  if (typeof v !== "string") return undefined;
  const out = cap(v, max);
  return out.trim() === "" ? undefined : out;
}

export function finite(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

// Capped list of the object items of `v`. `extra` counts items beyond the cap.
function records(v: unknown): { items: Rec[]; extra: number } {
  if (!Array.isArray(v)) return { items: [], extra: 0 };
  const items: Rec[] = [];
  for (const item of v.slice(0, MAX_LIST)) {
    const r = asRecord(item);
    if (r) items.push(r);
  }
  return { items, extra: Math.max(0, v.length - MAX_LIST) };
}

export type PlanAction = {
  actionId?: string;
  target?: string;
  value?: number;
  unit?: string;
  reason?: string;
};
export type PlanDetails = {
  planId?: string;
  planHash?: string;
  actions: PlanAction[];
  extraActions: number;
};

// plan_generated: { plan: { planId, planHash, actions: [{ actionId, target, value, unit, reason }] } }
export function readPlan(payload: unknown): PlanDetails {
  const plan = asRecord(asRecord(payload)?.plan);
  const { items, extra } = records(plan?.actions);
  const actions = items
    .map((a) => ({
      actionId: text(a.actionId),
      target: text(a.target),
      value: finite(a.value),
      unit: text(a.unit),
      reason: text(a.reason),
    }))
    .filter((a) => a.target !== undefined || a.value !== undefined || a.unit !== undefined);
  return { planId: text(plan?.planId), planHash: text(plan?.planHash), actions, extraActions: extra };
}

export type ValidatorError = {
  ruleId?: string;
  // null is a plan-level error with no single action or field (doc: "string | null").
  actionId?: string | null;
  field?: string | null;
  message?: string;
};
export type ValidatorDetails = {
  outcome?: string;
  validatorVersion?: string;
  errors: ValidatorError[];
  extraErrors: number;
};

// validator_result: { outcome, planId, validatorVersion, errors: [{ ruleId, actionId, field, message }] }
export function readValidator(payload: unknown): ValidatorDetails {
  const p = asRecord(payload);
  const { items, extra } = records(p?.errors);
  const errors = items
    .map((e) => ({
      ruleId: text(e.ruleId),
      actionId: e.actionId === null ? null : text(e.actionId),
      field: e.field === null ? null : text(e.field),
      message: text(e.message),
    }))
    .filter((e) => e.ruleId !== undefined || e.message !== undefined);
  return {
    outcome: text(p?.outcome),
    validatorVersion: text(p?.validatorVersion),
    errors,
    extraErrors: extra,
  };
}

export type CriticDetails = {
  outcome?: string;
  planHash?: string; // as recorded on the event
  responsePlanHash?: string; // the hash the critic recomputed (payload.response.planHash)
  reason?: string;
  triggeredRules: string[];
  extraRules: number;
  error?: { kind?: string; httpStatus?: number; detail?: string };
};

// critic_result: { outcome, planHash?, response?: { reason, triggeredRules, planHash? },
//                  error?: { kind, httpStatus?, detail? } }
export function readCritic(payload: unknown): CriticDetails {
  const p = asRecord(payload);
  const response = asRecord(p?.response);
  const rules = Array.isArray(response?.triggeredRules) ? response.triggeredRules : [];
  const triggeredRules = rules
    .slice(0, MAX_LIST)
    .map((r) => text(r))
    .filter((r): r is string => r !== undefined);
  const err = asRecord(p?.error);
  const error = err
    ? { kind: text(err.kind), httpStatus: finite(err.httpStatus), detail: text(err.detail) }
    : undefined;
  return {
    outcome: text(p?.outcome),
    planHash: text(p?.planHash),
    responsePlanHash: text(response?.planHash),
    reason: text(response?.reason),
    triggeredRules,
    extraRules: Math.max(0, rules.length - MAX_LIST),
    error:
      error && (error.kind !== undefined || error.httpStatus !== undefined || error.detail !== undefined)
        ? error
        : undefined,
  };
}

export type ExecutionDetails = {
  // Strictly boolean. Missing or any other type is undefined: no "dry run" label is shown.
  dryRun?: boolean;
  summary?: string; // execution_completed
  error?: string; // execution_failed
};

// execution_*: { dryRun, summary? } or { dryRun, error }
export function readExecution(payload: unknown): ExecutionDetails {
  const p = asRecord(payload);
  return {
    dryRun: typeof p?.dryRun === "boolean" ? p.dryRun : undefined,
    summary: text(p?.summary),
    error: text(p?.error),
  };
}

// command_received: { source }
export function readSource(payload: unknown): string | undefined {
  return text(asRecord(payload)?.source);
}

// Unknown event types: the raw payload as text, capped at MAX_RAW.
export function readRaw(payload: unknown): string | undefined {
  if (payload === undefined || payload === null) return undefined;
  try {
    const s = JSON.stringify(payload);
    return typeof s === "string" ? cap(s, MAX_RAW) : undefined;
  } catch {
    return undefined;
  }
}
