import { test } from "node:test";
import assert from "node:assert/strict";
import { summarize } from "./summary.ts";

const e = (eventType: string, payload: unknown) => ({ eventType, payload });
const vPass = e("validator_result", { outcome: "pass", errors: [] });
const cApprove = e("critic_result", { outcome: "approve" });

test("waiting for the worker", () => {
  assert.equal(summarize("received", []), "Waiting for the worker");
  assert.equal(summarize("received", [e("command_received", { source: "dashboard" })]), "Waiting for the worker");
  assert.equal(summarize(undefined, []), "Waiting for the worker");
});

test("rejected by validator names the first rule id", () => {
  const v = e("validator_result", {
    outcome: "fail",
    errors: [{ ruleId: "JOINT_LIMIT_EXCEEDED", actionId: "a1", field: "value", message: "m" }],
  });
  assert.equal(summarize("rejected", [v]), "Rejected by validator: JOINT_LIMIT_EXCEEDED");
  const two = e("validator_result", { outcome: "fail", errors: [{ ruleId: "A" }, { ruleId: "B" }] });
  assert.equal(summarize("rejected", [two]), "Rejected by validator: A (+1 more)");
});

test("rejected by critic lists triggered rules", () => {
  const c = e("critic_result", { outcome: "reject", response: { triggeredRules: ["joint_limit_exceeded"] } });
  assert.equal(summarize("rejected", [vPass, c]), "Rejected by critic: joint_limit_exceeded");
  const two = e("critic_result", { outcome: "reject", response: { triggeredRules: ["a", "b"] } });
  assert.equal(summarize("rejected", [two]), "Rejected by critic: a, b");
});

test("critic error: nothing executed", () => {
  const c = e("critic_result", { outcome: "error", error: { kind: "timeout" } });
  assert.equal(summarize("rejected", [vPass, c]), "Critic unavailable: nothing executed");
});

test("earliest rejecting event wins", () => {
  const v = e("validator_result", { outcome: "fail", errors: [{ ruleId: "UNKNOWN_JOINT" }] });
  const c = e("critic_result", { outcome: "reject", response: { triggeredRules: ["x"] } });
  assert.equal(summarize("rejected", [v, c]), "Rejected by validator: UNKNOWN_JOINT");
  assert.equal(summarize("rejected", [c, v]), "Rejected by critic: x");
});

test("rejection without a usable reason still says who rejected", () => {
  assert.equal(summarize("rejected", [e("validator_result", { outcome: "fail", errors: "boom" })]), "Rejected by validator");
  assert.equal(summarize("rejected", [e("critic_result", { outcome: "reject" })]), "Rejected by critic");
});

test("execution outcomes, dry run labelled", () => {
  const started = e("execution_started", { dryRun: true });
  assert.equal(summarize("executing", [vPass, cApprove, started]), "Executing (dry run)");
  assert.equal(
    summarize("completed", [vPass, cApprove, started, e("execution_completed", { dryRun: true })]),
    "Dry run completed",
  );
  assert.equal(summarize("completed", [e("execution_completed", { dryRun: false })]), "Execution completed");
  assert.equal(summarize("failed", [started, e("execution_failed", { dryRun: true, error: "x" })]), "Dry run failed");
  assert.equal(summarize("failed", [e("execution_failed", { dryRun: false, error: "x" })]), "Execution failed");
});

test("dryRun that is not a boolean is not treated as a dry run", () => {
  assert.equal(summarize("completed", [e("execution_completed", { dryRun: "true" })]), "Execution completed");
});

test("approved and waiting, and partial checks", () => {
  assert.equal(summarize("approved", [vPass, cApprove]), "Approved, waiting for execution");
  assert.equal(summarize("received", [vPass]), "Waiting for the critic");
  assert.equal(summarize("received", [cApprove]), "Waiting for the validator");
  assert.equal(
    summarize("received", [e("plan_generated", { plan: {} })]),
    "Plan generated, waiting for validator and critic",
  );
});

test("status is the fallback when events do not explain it", () => {
  assert.equal(summarize("rejected", []), "Rejected (reason not recorded)");
  assert.equal(summarize("failed", []), "Execution failed");
  assert.equal(summarize("completed", []), "Completed");
  assert.equal(summarize("executing", []), "Executing");
  assert.equal(summarize("approved", []), "Approved, waiting for execution");
});

test("events win over status", () => {
  const c = e("critic_result", { outcome: "reject", response: { triggeredRules: ["x"] } });
  assert.equal(summarize("received", [c]), "Rejected by critic: x");
});

test("malformed events and statuses never throw", () => {
  const bad = [
    e("validator_result", null),
    e("critic_result", [1]),
    e("mystery", { a: 1 }),
    null as unknown as { eventType: string; payload: unknown },
    { eventType: 5 as unknown as string, payload: undefined },
  ];
  assert.equal(summarize("received", bad), "Waiting for the worker");
  assert.equal(summarize(42 as unknown as string, bad), "Waiting for the worker");
  // The event type alone says execution finished, even if its payload is garbage.
  assert.equal(summarize("completed", [e("execution_completed", "x")]), "Execution completed");
});
