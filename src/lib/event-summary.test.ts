import { test } from "node:test";
import assert from "node:assert/strict";
import { actionTargets, eventContext, isRedundant, shortHash, summarizeEvent } from "./event-summary.ts";

const e = (eventType: unknown, payload: unknown) => ({ eventType, payload });
const H = "sha256:" + "ab".repeat(32);
const act = (id: string, target: string, value: number) => ({ actionId: id, type: "set_joint", target, value, unit: "rad" });
const plan = (planId: string | undefined, ...actions: unknown[]) =>
  e("plan_generated", { plan: { planId, planHash: H, actions } });
const text = (x: ReturnType<typeof summarizeEvent>) => x?.text;

test("shortHash: 8 hex characters after the prefix, plus an ellipsis", () => {
  assert.equal(shortHash(H), "abababab…");
  assert.equal(shortHash("sha256:ABCDEF0123456789"), "abcdef01…");
  assert.equal(shortHash("0123456789abcdef"), "01234567…");
  for (const bad of ["sha256:xyz", "sha256:abc", "", "zzzzzzzzzz", null, undefined, 5, {}, []]) {
    assert.equal(shortHash(bad), undefined);
  }
});

test("plan_generated: actions and the short plan hash", () => {
  const one = summarizeEvent(plan("p1", act("a1", "_6", -1)));
  assert.deepEqual(one, { text: "_6 -> -1 rad", hash: H, shortHash: "abababab…" });
  assert.equal(text(summarizeEvent(plan("p1", act("a1", "_1", 0.25), act("a2", "_2", 1)))), "_1 -> 0.25 rad, _2 -> 1 rad");
});

test("plan_generated: caps at 3 actions then +N more", () => {
  const five = [1, 2, 3, 4, 5].map((n) => act(`a${n}`, `_${n}`, n));
  assert.equal(text(summarizeEvent(plan("p", ...five))), "_1 -> 1 rad, _2 -> 2 rad, _3 -> 3 rad +2 more");
});

test("plan_generated: planner failure and missing pieces", () => {
  assert.equal(text(summarizeEvent(e("plan_generated", { outcome: "error", error: { kind: "unsupported_command" } }))), "Not a supported command");
  assert.equal(text(summarizeEvent(e("plan_generated", { outcome: "error", error: { kind: "planner_error" } }))), "Planner error");
  assert.equal(text(summarizeEvent(e("plan_generated", { outcome: "error", error: { kind: "planner_errr" } }))), "Planner error · planner_errr");
  assert.equal(text(summarizeEvent(e("plan_generated", { outcome: "error", error: { kind: 5 } }))), "Planner error");
  assert.equal(text(summarizeEvent(plan("p", { target: "_6" }))), "_6");
  assert.equal(text(summarizeEvent(plan("p", { value: 2, unit: "rad" }))), "2 rad");
  // hash only
  const hashOnly = summarizeEvent(e("plan_generated", { plan: { planHash: H, actions: [{ actionId: "a" }] } }));
  assert.equal(hashOnly?.text, "");
  assert.equal(hashOnly?.shortHash, "abababab…");
  // nothing informative at all
  assert.equal(summarizeEvent(e("plan_generated", { plan: { actions: [] } })), undefined);
  // a bad hash is dropped, not shown
  assert.equal(summarizeEvent(e("plan_generated", { plan: { planHash: "nope", actions: [] } })), undefined);
});

test("validator pass: action count from the matching plan, version", () => {
  const ctx = eventContext([plan("p1", act("a1", "_1", 1)), plan("p2", act("a1", "_1", 1), act("a2", "_2", 1))]);
  const pass = (extra: object) => e("validator_result", { outcome: "pass", errors: [], ...extra });
  assert.equal(text(summarizeEvent(pass({ planId: "p1", validatorVersion: "0.1.0" }), ctx)), "1 action(s) checked · validator v0.1.0");
  assert.equal(text(summarizeEvent(pass({ planId: "p2", validatorVersion: "0.1.0" }), ctx)), "2 action(s) checked · validator v0.1.0");
  // unknown or missing planId falls back to the latest plan
  assert.equal(text(summarizeEvent(pass({ planId: "zzz", validatorVersion: "1" }), ctx)), "2 action(s) checked · validator v1");
  assert.equal(text(summarizeEvent(pass({ validatorVersion: "1" }), ctx)), "2 action(s) checked · validator v1");
  // no plan event: the count is left out
  assert.equal(text(summarizeEvent(pass({ validatorVersion: "1" }), eventContext([]))), "validator v1");
  assert.equal(text(summarizeEvent(pass({ validatorVersion: "1" }))), "validator v1");
  // nothing known: no line
  assert.equal(summarizeEvent(pass({}), eventContext([])), undefined);
  // planner failures do not count as a plan
  assert.equal(eventContext([e("plan_generated", { outcome: "error", error: { kind: "x" } })]).plans.length, 0);
});

test("validator fail: rule ids with the joint", () => {
  const ctx = eventContext([plan("p", act("a1", "_6", -1))]);
  const fail = e("validator_result", {
    outcome: "fail",
    validatorVersion: "0.1.0",
    errors: [
      { ruleId: "JOINT_LIMIT_EXCEEDED", actionId: "a1", field: "value" },
      { ruleId: "MAX_ACTIONS_EXCEEDED", actionId: null, field: null },
    ],
  });
  assert.equal(text(summarizeEvent(fail, ctx)), "JOINT_LIMIT_EXCEEDED on _6, MAX_ACTIONS_EXCEEDED");
  assert.equal(text(summarizeEvent(fail)), "JOINT_LIMIT_EXCEEDED, MAX_ACTIONS_EXCEEDED");
  const many = e("validator_result", { outcome: "fail", errors: [1, 2, 3, 4, 5].map((n) => ({ ruleId: `R${n}`, actionId: null, field: null })) });
  assert.equal(text(summarizeEvent(many)), "R1, R2, R3 +2 more");
  assert.equal(summarizeEvent(e("validator_result", { outcome: "fail", errors: [{ message: "only a message" }] })), undefined);
  assert.equal(summarizeEvent(e("validator_result", { outcome: "fail", errors: [] })), undefined);
});

test("critic approve: rule count plus short hash", () => {
  const approve = (response: unknown, planHash: unknown = H) => e("critic_result", { outcome: "approve", planHash, response });
  assert.deepEqual(summarizeEvent(approve({ triggeredRules: [] })), { text: "0 rules triggered", hash: H, shortHash: "abababab…" });
  assert.equal(text(summarizeEvent(approve({ triggeredRules: ["a"] }))), "1 rule triggered");
  // hash falls back to the response hash
  assert.equal(summarizeEvent(approve({ triggeredRules: [], planHash: H }, undefined))?.shortHash, "abababab…");
  // no array: no count, hash only
  const hashOnly = summarizeEvent(approve({}));
  assert.equal(hashOnly?.text, "");
  assert.equal(hashOnly?.shortHash, "abababab…");
  // no array and no usable hash: no line
  assert.equal(summarizeEvent(approve({}, "bad")), undefined);
  assert.equal(summarizeEvent(e("critic_result", { outcome: "approve" })), undefined);
});

test("critic reject and error", () => {
  assert.equal(text(summarizeEvent(e("critic_result", { outcome: "reject", response: { triggeredRules: ["joint_limit_exceeded", "plan_hash_mismatch"] } }))), "joint_limit_exceeded, plan_hash_mismatch");
  assert.equal(text(summarizeEvent(e("critic_result", { outcome: "reject", response: { triggeredRules: ["a", "b", "c", "d", "e"] } }))), "a, b, c +2 more");
  assert.equal(summarizeEvent(e("critic_result", { outcome: "reject", response: { triggeredRules: [1, null] } })), undefined);
  assert.equal(summarizeEvent(e("critic_result", { outcome: "reject", planHash: H })), undefined);
  assert.equal(text(summarizeEvent(e("critic_result", { outcome: "error", error: { kind: "http_status" } }))), "Critic returned an HTTP error");
  assert.equal(text(summarizeEvent(e("critic_result", { outcome: "error", error: { kind: "timeout" } }))), "Critic timed out");
  assert.equal(text(summarizeEvent(e("critic_result", { outcome: "error", error: { kind: "invalid_body" } }))), "Critic returned an invalid response");
  assert.equal(summarizeEvent(e("critic_result", { outcome: "error", error: { kind: 3 } })), undefined);
  assert.equal(summarizeEvent(e("critic_result", { outcome: "weird" })), undefined);
});

test("execution events and command_received", () => {
  assert.equal(text(summarizeEvent(e("execution_started", { dryRun: true }))), "dry run");
  assert.equal(text(summarizeEvent(e("execution_completed", { dryRun: false }))), "executed");
  assert.equal(summarizeEvent(e("execution_completed", { dryRun: "true" })), undefined);
  assert.equal(text(summarizeEvent(e("command_received", { source: "dashboard" }))), "dashboard");
  assert.equal(summarizeEvent(e("command_received", { source: 3 })), undefined);
});

test("malformed payloads never throw", () => {
  const types = ["plan_generated", "validator_result", "critic_result", "execution_started", "command_received"];
  for (const t of types) {
    for (const p of [null, undefined, [], "x", 5, true, { plan: "x" }, { errors: "x" }, { response: [] }, { response: "x" }, { error: 7 }, { outcome: "pass", planId: {} }, { outcome: "fail", errors: [null, 3, "x"] }]) {
      assert.doesNotThrow(() => summarizeEvent(e(t, p), eventContext([e("plan_generated", p), null as never])));
    }
  }
  assert.equal(summarizeEvent(e("plan_generated", { plan: { actions: "nope" } })), undefined);
  assert.equal(summarizeEvent(e("something_else", { a: 1 })), undefined);
  assert.equal(summarizeEvent(e(42, {})), undefined);
  assert.equal(summarizeEvent(null as never), undefined);
});

test("long and hostile strings are capped and cleaned", () => {
  const long = "x".repeat(1000);
  const line = text(summarizeEvent(e("critic_result", { outcome: "error", error: { kind: long } })));
  assert.ok(line !== undefined && line.length <= 120 && line.endsWith("…"));
  assert.equal(text(summarizeEvent(e("command_received", { source: "a‮b\u0000c" }))), "a b c");
});

test("actionTargets: ignores bad plans and duplicates", () => {
  const m = actionTargets([plan("p", act("a1", "_1", 1), act("a1", "_2", 1)), e("plan_generated", null), e("x", {}), null as never]);
  assert.equal(m.get("a1"), "_1");
  assert.equal(m.size, 1);
});

test("isRedundant: only when the summary repeats the title", () => {
  assert.equal(isRedundant("Dry run completed", "dry run"), true);
  assert.equal(isRedundant("Dry run started: nothing is sent to a robot", "dry run"), true);
  assert.equal(isRedundant("Command received from dashboard", "dashboard"), true);
  assert.equal(isRedundant("Command received", "command received."), true);
  // stems
  assert.equal(isRedundant("Critic approved the plan", "approve"), true);
  assert.equal(isRedundant("Execution completed", "executed"), true);
  // informative lines stay
  assert.equal(isRedundant("Command received", "dashboard"), false);
  assert.equal(isRedundant("Validator passed", "1 action(s) checked · validator v0.1.0"), false);
  assert.equal(isRedundant("Critic rejected", "joint_limit_exceeded, plan_hash_mismatch"), false);
  assert.equal(isRedundant("Plan generated", "_1 -> 0.5 rad"), false);
  assert.equal(isRedundant("Execution started", "executed"), true);
  // malformed
  for (const [t, s] of [["x", ""], ["", "x"], [null, "x"], ["x", undefined], ["!!!", "???"], [{}, []], [5, 5]] as const) {
    assert.equal(isRedundant(t, s), false);
  }
});
