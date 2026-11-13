// One-line outcome summary for the selected command. Pure.
// Events come first, the command status is only a fallback when events do not explain it.
// Events are expected oldest first (see lib/merge.ts).
import { readCritic, readExecution, readValidator, text } from "./payload.ts";

type EventLike = { eventType: string; payload: unknown };

function more(extra: number): string {
  return extra > 0 ? ` (+${extra} more)` : "";
}

export function summarize(status: string | undefined, events: EventLike[]): string {
  let validator: "pass" | "fail" | undefined;
  let critic: "approve" | "reject" | "error" | undefined;
  let hasPlan = false;
  let started: boolean | undefined;
  let terminal: string | undefined;

  for (const e of events) {
    switch (e?.eventType) {
      case "plan_generated":
        hasPlan = true;
        break;
      case "validator_result": {
        const v = readValidator(e.payload);
        if (v.outcome === "fail") {
          // The earliest rejecting event wins: the worker may stop at the first failure.
          const first = v.errors.find((x) => x.ruleId !== undefined)?.ruleId;
          return first
            ? `Rejected by validator: ${first}${more(v.errors.length + v.extraErrors - 1)}`
            : "Rejected by validator";
        }
        if (v.outcome === "pass") validator = "pass";
        break;
      }
      case "critic_result": {
        const c = readCritic(e.payload);
        if (c.outcome === "reject") {
          return c.triggeredRules.length > 0
            ? `Rejected by critic: ${c.triggeredRules.join(", ")}${more(c.extraRules)}`
            : "Rejected by critic";
        }
        if (c.outcome === "error") return "Critic unavailable: nothing executed";
        if (c.outcome === "approve") critic = "approve";
        break;
      }
      case "execution_started":
        started = readExecution(e.payload).dryRun === true;
        break;
      case "execution_completed": {
        terminal = readExecution(e.payload).dryRun === true ? "Dry run completed" : "Execution completed";
        break;
      }
      case "execution_failed": {
        terminal = readExecution(e.payload).dryRun === true ? "Dry run failed" : "Execution failed";
        break;
      }
    }
  }

  if (terminal) return terminal;
  if (started !== undefined) return started ? "Executing (dry run)" : "Executing";
  if (validator === "pass" && critic === "approve") return "Approved, waiting for execution";

  // Events do not explain it yet. Fall back to the command status.
  switch (text(status)) {
    case "rejected":
      return "Rejected (reason not recorded)";
    case "failed":
      return "Execution failed";
    case "completed":
      return "Completed";
    case "executing":
      return "Executing";
    case "approved":
      return "Approved, waiting for execution";
  }

  if (validator === "pass") return "Waiting for the critic";
  if (critic === "approve") return "Waiting for the validator";
  if (hasPlan) return "Plan generated, waiting for validator and critic";
  return "Waiting for the worker";
}
