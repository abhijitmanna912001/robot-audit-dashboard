// Display data for the verdict card and the stepper, derived ONLY from audit events. Pure.
// Payloads are untrusted: everything goes through the defensive readers in payload.ts, a missing
// piece is left out (never invented), and nothing throws.
import { elapsedMs, formatElapsed, workerWaitLabel } from "./elapsed.ts";
import { eventContext } from "./event-summary.ts";
import { kindLabel } from "./kind-label.ts";
import { readCritic, readExecution, readPlanFailure, readValidator, asRecord } from "./payload.ts";
import { bannerTone, derivePipeline, STATE_WORD, type StageName } from "./pipeline.ts";

type EventLike = { eventType: unknown; payload: unknown };

const list = (events: unknown): EventLike[] => (Array.isArray(events) ? (events as EventLike[]) : []);
// Non-breaking spaces between a number and its unit, so "5 s" or "2 min 5 s" never split across
// lines. A unit that is followed by another number ("2 min 5 s") is bound to it as well.
export function bindUnits(s: string): string {
  return s.replace(/(\d) (?=[A-Za-z])/g, "$1\u00A0").replace(/\b(min|h|of) (?=\d)/g, "$1\u00A0");
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function last(events: EventLike[], type: string, pick?: (p: unknown) => boolean): EventLike | undefined {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i];
    if (e?.eventType === type && (!pick || pick(e.payload))) return e;
  }
  return undefined;
}

// Rules triggered by the critic. Only when the response really carries a triggeredRules array,
// so a missing array is never shown as "0 rules".
function criticRuleCount(events: EventLike[]): number | undefined {
  const e = last(events, "critic_result", (p) => {
    const o = readCritic(p).outcome;
    return o === "approve" || o === "reject";
  });
  if (!e || !Array.isArray(asRecord(asRecord(e.payload)?.response)?.triggeredRules)) return undefined;
  const c = readCritic(e.payload);
  return c.triggeredRules.length + c.extraRules;
}

export function verdictStamp(status: string | undefined, events: unknown): string {
  try {
    const list_ = list(events);
    const stages = derivePipeline(list_ as never);
    const tone = bannerTone(status, list_ as never);
    const execution = stages.find((s) => s.stage === "execution")?.state;
    switch (tone) {
      case "blocked":
        return "REJECTED";
      case "broke":
        return "FAILED";
      case "success":
        return execution === "dry_run" ? "APPROVED · DRY RUN" : "COMPLETED";
      case "running": {
        const started = last(list_, "execution_started");
        return started && readExecution(started.payload).dryRun === true ? "RUNNING · DRY RUN" : "RUNNING";
      }
      default:
        return "WAITING";
    }
  } catch {
    return "WAITING";
  }
}

// Facts under the headline. Each piece is present only when its source is.
export function verdictFacts(events: unknown): string[] {
  try {
    const list_ = list(events);
    const out: string[] = [];
    const stages = derivePipeline(list_ as never);
    const checks = stages.filter(
      (s) => (s.stage === "validator" || s.stage === "critic") && s.state !== "not_reached",
    );
    if (checks.length > 0) {
      out.push(`${checks.filter((s) => s.state === "passed").length} of ${checks.length} checks passed`);
    }
    const rules = criticRuleCount(list_);
    if (rules !== undefined) out.push(`${plural(rules, "rule")} triggered`);
    if (stages.find((s) => s.stage === "validator")?.state === "failed") {
      const v = last(list_, "validator_result", (p) => readValidator(p).outcome === "fail");
      if (v) {
        const d = readValidator(v.payload);
        const n = d.errors.length + d.extraErrors;
        if (n > 0) out.push(plural(n, "validator error"));
      }
    }
    const ms = elapsedMs(list_ as never);
    const span = ms === undefined ? undefined : formatElapsed(ms);
    if (span !== undefined) out.push(`${span} elapsed`);
    const wait = workerWaitLabel(list_ as never);
    if (wait !== undefined) out.push(wait);
    return out.map(bindUnits);
  } catch {
    return [];
  }
}

// One short line under each stepper node.
export function stageDetails(events: unknown): Partial<Record<StageName, string>> {
  const out: Partial<Record<StageName, string>> = {};
  try {
    const list_ = list(events);
    const stages = derivePipeline(list_ as never);
    const state = (n: StageName) => stages.find((s) => s.stage === n)?.state;
    const ctx = eventContext(list_ as never);

    // Planned
    const plan = last(list_, "plan_generated");
    if (plan) {
      const failure = readPlanFailure(plan.payload);
      if (failure) {
        const label = kindLabel(failure.kind);
        if (label) out.planned = label;
      } else if (ctx.plans.length > 0) {
        out.planned = plural(ctx.plans[ctx.plans.length - 1].actionCount, "action");
      }
    }

    // Validator
    const v = last(list_, "validator_result");
    if (v) {
      const d = readValidator(v.payload);
      if (state("validator") === "passed") {
        const n = ctx.plans.length > 0 ? ctx.plans[ctx.plans.length - 1].actionCount : undefined;
        const bits = [
          d.validatorVersion ? `v${d.validatorVersion}` : undefined,
          n === undefined ? undefined : `${plural(n, "action")} checked`,
        ].filter((x) => x !== undefined);
        if (bits.length > 0) out.validator = bits.join(" · ");
      } else if (state("validator") === "failed") {
        const f = last(list_, "validator_result", (p) => readValidator(p).outcome === "fail");
        const fd = f ? readValidator(f.payload) : d;
        const n = fd.errors.length + fd.extraErrors;
        if (n > 0) out.validator = plural(n, "error");
      }
    }

    // Critic
    if (state("critic") === "error") {
      const e = last(list_, "critic_result", (p) => readCritic(p).outcome === "error");
      const label = e ? kindLabel(readCritic(e.payload).error?.kind) : undefined;
      if (label) out.critic = label;
    } else if (state("critic") === "passed" || state("critic") === "failed") {
      const n = criticRuleCount(list_);
      if (n !== undefined) out.critic = `${plural(n, "rule")} triggered`;
    }

    // Execution
    if (state("execution") !== "not_reached") {
      const e =
        last(list_, "execution_completed") ?? last(list_, "execution_failed") ?? last(list_, "execution_started");
      const dry = e ? readExecution(e.payload).dryRun : undefined;
      if (dry !== undefined) {
        // A finished dry run says what it means; a repeated "dry run" under "dry run" is dropped below.
        out.execution = !dry ? "executed" : state("execution") === "dry_run" ? "nothing sent to a robot" : "dry run";
      }
    }
    // A detail must not repeat the line above it (the state word).
    const norm = (v: string) => v.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    for (const s of stages) {
      const d = out[s.stage];
      if (d !== undefined && norm(d) === norm(STATE_WORD[s.state])) delete out[s.stage];
    }
  } catch {
    // Details are optional.
  }
  return out;
}
