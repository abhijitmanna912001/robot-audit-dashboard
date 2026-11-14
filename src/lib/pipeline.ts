// Pipeline stage states for one command, derived ONLY from its audit events. Pure.
// Stages: Planned, Validator, Critic, Execution. Never throws: payloads are untrusted
// (see lib/payload.ts), so every field is read through the defensive readers there.
// Precedence mirrors lib/summary.ts so the banner colour and the summary text agree.
import { readCritic, readExecution, readPlanFailure, readValidator } from "./payload.ts";

export const STAGES = ["planned", "validator", "critic", "execution"] as const;
export type StageName = (typeof STAGES)[number];

export type StageState = "passed" | "failed" | "error" | "not_reached" | "dry_run" | "running";

export type PipelineStage = { stage: StageName; state: StageState };

export type BannerTone = "success" | "blocked" | "broke" | "running" | "waiting";

type EventLike = { eventType: string; payload: unknown };

type Facts = {
  planned: "none" | "passed" | "error"; // latest plan_generated wins
  validatorFail: boolean; // any validator fail is sticky
  validatorPass: boolean; // latest recognised validator outcome is pass
  criticReject: boolean; // any critic reject is sticky
  criticLast: "approve" | "error" | undefined; // latest recognised non-reject outcome
  terminal: "completed" | "failed" | undefined; // latest terminal execution event
  terminalDryRun: boolean; // dryRun === true on that terminal event
  started: boolean;
};

// One pass over the events. Anything unrecognised is ignored: no usable evidence.
function analyze(events: readonly EventLike[]): Facts {
  const f: Facts = {
    planned: "none",
    validatorFail: false,
    validatorPass: false,
    criticReject: false,
    criticLast: undefined,
    terminal: undefined,
    terminalDryRun: false,
    started: false,
  };
  if (!Array.isArray(events)) return f;

  for (const e of events) {
    switch (e?.eventType) {
      case "plan_generated":
        f.planned = readPlanFailure(e.payload) ? "error" : "passed";
        break;
      case "validator_result": {
        const outcome = readValidator(e.payload).outcome;
        if (outcome === "fail") {
          f.validatorFail = true;
          f.validatorPass = false;
        } else if (outcome === "pass" && !f.validatorFail) {
          f.validatorPass = true;
        }
        break;
      }
      case "critic_result": {
        const outcome = readCritic(e.payload).outcome;
        if (outcome === "reject") f.criticReject = true;
        else if (outcome === "approve" || outcome === "error") f.criticLast = outcome;
        break;
      }
      case "execution_started":
        f.started = true;
        break;
      case "execution_completed":
        f.terminal = "completed";
        f.terminalDryRun = readExecution(e.payload).dryRun === true;
        break;
      case "execution_failed":
        f.terminal = "failed";
        f.terminalDryRun = readExecution(e.payload).dryRun === true;
        break;
    }
  }
  return f;
}

const NOT_REACHED: PipelineStage[] = STAGES.map((stage) => ({ stage, state: "not_reached" }));

export function derivePipeline(events: readonly EventLike[]): PipelineStage[] {
  try {
    const f = analyze(events);
    const plannedFailed = f.planned === "error";

    const planned: StageState =
      f.planned === "none" ? "not_reached" : plannedFailed ? "error" : "passed";

    const validator: StageState = plannedFailed
      ? "not_reached"
      : f.validatorFail
        ? "failed"
        : f.validatorPass
          ? "passed"
          : "not_reached";

    const critic: StageState = plannedFailed
      ? "not_reached"
      : f.criticReject
        ? "failed"
        : f.criticLast === "approve"
          ? "passed"
          : f.criticLast === "error"
            ? "error"
            : "not_reached";

    // Nothing runs after a planner error, a validator failure or a critic reject/error,
    // so Execution is "not reached" there even if stray execution events exist (the
    // timeline still lists every event).
    let execution: StageState;
    if (plannedFailed || validator === "failed" || critic === "failed" || critic === "error") {
      execution = "not_reached";
    } else if (f.terminal === "failed") {
      execution = "failed";
    } else if (f.terminal === "completed") {
      execution = f.terminalDryRun ? "dry_run" : "passed";
    } else if (f.started) {
      execution = "running";
    } else {
      execution = "not_reached";
    }

    return [
      { stage: "planned", state: planned },
      { stage: "validator", state: validator },
      { stage: "critic", state: critic },
      { stage: "execution", state: execution },
    ];
  } catch {
    return NOT_REACHED.map((s) => ({ ...s }));
  }
}

// Colour family for the summary banner. Red = blocked (validator fail, critic reject),
// amber = something broke (execution failed, planner error, critic error), green = done,
// info = running, muted = waiting. Same order as summarize(), so tone and text agree.
export function bannerTone(status: string | undefined, events: readonly EventLike[]): BannerTone {
  try {
    const f = analyze(events);
    if (f.validatorFail || f.criticReject) return "blocked";
    if (f.terminal === "completed") return "success";
    if (f.terminal === "failed") return "broke";
    if (f.started) return "running";
    if (f.planned === "error") return "broke";
    if (f.criticLast === "error") return "broke";
    if (f.validatorPass && f.criticLast === "approve") return "waiting";
  } catch {
    // fall through to the status fallback
  }
  switch (status) {
    case "rejected":
      return "blocked";
    case "failed":
      return "broke";
    case "completed":
      return "success";
    case "executing":
      return "running";
    default:
      return "waiting";
  }
}

// The step to mark aria-current: the one still running (or in a dry run), otherwise the one
// where the pipeline stopped (failed or error). None when it is all done or nothing started.
export function activeStage(stages: readonly PipelineStage[]): StageName | undefined {
  return (
    stages.find((s) => s.state === "running" || s.state === "dry_run")?.stage ??
    stages.find((s) => s.state === "failed" || s.state === "error")?.stage
  );
}
