import { test } from "node:test";
import assert from "node:assert/strict";
import { actionTargets, isRedundant, summarizeEvent } from "./event-summary.ts";

const e = (eventType: unknown, payload: unknown) => ({ eventType, payload });
const act = (id: string, target: string, value: number) => ({ actionId: id, type: "set_joint", target, value, unit: "rad" });
const plan = (...actions: unknown[]) => e("plan_generated", { plan: { planId: "p", actions } });

test("plan_generated: actions", () => {
  assert.equal(summarizeEvent(plan(act("a1", "_6", -1))), "_6 -> -1 rad");
  assert.equal(summarizeEvent(plan(act("a1", "_1", 0.25), act("a2", "_2", 1))), "_1 -> 0.25 rad, _2 -> 1 rad");
});

test("plan_generated: caps at 3 actions then +N more", () => {
  const five = [1, 2, 3, 4, 5].map((n) => act(`a${n}`, `_${n}`, n));
  assert.equal(summarizeEvent(plan(...five)), "_1 -> 1 rad, _2 -> 2 rad, _3 -> 3 rad +2 more");
});

test("plan_generated: planner failure", () => {
  assert.equal(
    summarizeEvent(e("plan_generated", { outcome: "error", error: { kind: "unsupported_command", detail: "x" } })),
    "planner error · unsupported_command",
  );
  assert.equal(summarizeEvent(e("plan_generated", { outcome: "error" })), "planner error");
  assert.equal(summarizeEvent(e("plan_generated", { outcome: "error", error: { kind: 5 } })), "planner error");
});

test("plan_generated: missing pieces are dropped, never invented", () => {
  assert.equal(summarizeEvent(plan({ target: "_6" })), "_6");
  assert.equal(summarizeEvent(plan({ value: 2, unit: "rad" })), "2 rad");
  assert.equal(summarizeEvent(plan({ actionId: "a1" })), undefined);
  assert.equal(summarizeEvent(plan(act("a1", "_1", NaN))), "_1 -> rad");
});

test("validator_result: outcome, version, first rule and its joint", () => {
  const targets = actionTargets([plan(act("a1", "_6", -1))]);
  const fail = e("validator_result", {
    outcome: "fail",
    validatorVersion: "0.1.0",
    errors: [{ ruleId: "JOINT_LIMIT_EXCEEDED", actionId: "a1", field: "value" }, { ruleId: "OTHER", actionId: null, field: null }],
  });
  assert.equal(summarizeEvent(fail, targets), "fail · v0.1.0 · JOINT_LIMIT_EXCEEDED on _6");
  assert.equal(summarizeEvent(fail), "fail · v0.1.0 · JOINT_LIMIT_EXCEEDED");
  assert.equal(
    summarizeEvent(e("validator_result", { outcome: "fail", errors: [{ ruleId: "EMPTY_ACTIONS", actionId: null, field: null }] }), targets),
    "fail · EMPTY_ACTIONS",
  );
  assert.equal(summarizeEvent(e("validator_result", { outcome: "pass", validatorVersion: "1", errors: [] })), "pass · v1");
});

test("critic_result: outcome plus first rule or error kind", () => {
  assert.equal(
    summarizeEvent(e("critic_result", { outcome: "reject", response: { triggeredRules: ["R1", "R2"] } })),
    "reject · R1",
  );
  assert.equal(summarizeEvent(e("critic_result", { outcome: "error", error: { kind: "timeout" } })), "error · timeout");
  assert.equal(summarizeEvent(e("critic_result", { outcome: "approve" })), "approve");
});

test("execution events: dry run or executed, nothing when dryRun is not a boolean", () => {
  assert.equal(summarizeEvent(e("execution_started", { dryRun: true })), "dry run");
  assert.equal(summarizeEvent(e("execution_completed", { dryRun: false })), "executed");
  assert.equal(summarizeEvent(e("execution_completed", { dryRun: "true" })), undefined);
  assert.equal(summarizeEvent(e("execution_completed", {})), undefined);
});

test("command_received: source", () => {
  assert.equal(summarizeEvent(e("command_received", { source: "dashboard" })), "dashboard");
  assert.equal(summarizeEvent(e("command_received", { source: 3 })), undefined);
});

test("malformed payloads never throw and give no invented text", () => {
  const types = ["plan_generated", "validator_result", "critic_result", "execution_started", "command_received"];
  for (const t of types) {
    for (const p of [null, undefined, [], "x", 5, true, { plan: "x" }, { errors: "x" }, { response: [] }, { error: 7 }]) {
      assert.doesNotThrow(() => summarizeEvent(e(t, p)));
    }
  }
  assert.equal(summarizeEvent(e("plan_generated", { plan: { actions: "nope" } })), undefined);
  assert.equal(summarizeEvent(e("validator_result", { errors: [null, 3, "x"] })), undefined);
  assert.equal(summarizeEvent(e("critic_result", { response: { triggeredRules: [1, null] } })), undefined);
});

test("unknown or odd event types and inputs", () => {
  assert.equal(summarizeEvent(e("something_else", { a: 1 })), undefined);
  assert.equal(summarizeEvent(e(42, {})), undefined);
  assert.equal(summarizeEvent(null as never), undefined);
});

test("long and hostile strings are capped and cleaned", () => {
  const long = "x".repeat(1000);
  const line = summarizeEvent(e("critic_result", { outcome: "error", error: { kind: long } }));
  assert.ok(line !== undefined && line.length <= 120);
  assert.ok(line.endsWith("…"));
  const hostile = summarizeEvent(e("command_received", { source: "a‮b\u0000c" }));
  assert.equal(hostile, "a b c");
});

test("actionTargets: ignores bad plans and duplicates", () => {
  const m = actionTargets([plan(act("a1", "_1", 1), act("a1", "_2", 1)), e("plan_generated", null), e("x", {}), null as never]);
  assert.equal(m.get("a1"), "_1");
  assert.equal(m.size, 1);
});

test("isRedundant: only when the summary repeats the title", () => {
  assert.equal(isRedundant("Critic approved", "approve"), false);
  assert.equal(isRedundant("Execution started", "dry run"), false);
  assert.equal(isRedundant("Dry run completed", "dry run"), true);
  assert.equal(isRedundant("Command received", "command received."), true);
  assert.equal(isRedundant("Validator passed", "pass · v1"), false);
  assert.equal(isRedundant("x", ""), false);
  assert.equal(isRedundant("", "x"), false);
  assert.equal(isRedundant(null, "x"), false);
  assert.equal(isRedundant("x", undefined), false);
  assert.equal(isRedundant("!!!", "???"), false);
});
