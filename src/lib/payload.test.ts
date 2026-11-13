import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MAX_LIST,
  MAX_RAW,
  MAX_STRING,
  readCritic,
  readExecution,
  readPlan,
  readPlanFailure,
  readRaw,
  readSource,
  readValidator,
  text,
} from "./payload.ts";

const junk: unknown[] = [null, undefined, 42, "str", true, [], [1, 2], {}];

test("every reader survives non-object payloads", () => {
  for (const p of junk) {
    assert.deepEqual(readPlan(p).actions, []);
    assert.deepEqual(readValidator(p).errors, []);
    assert.deepEqual(readCritic(p).triggeredRules, []);
    assert.equal(readExecution(p).dryRun, undefined);
    assert.equal(readSource(p), undefined);
  }
});

test("text caps length, strips control and bidi characters, rejects non-strings", () => {
  assert.equal(text("a".repeat(MAX_STRING)), "a".repeat(MAX_STRING));
  assert.equal(text("a".repeat(MAX_STRING + 1)), "a".repeat(MAX_STRING) + "…");
  assert.equal(text("a\u0000b‮c\nd"), "a b c d");
  assert.equal(text("   "), undefined);
  for (const v of [1, null, {}, [], true]) assert.equal(text(v), undefined);
});

test("text does not split a surrogate pair at the cap", () => {
  const s = "a".repeat(MAX_STRING - 1) + "😀";
  const out = text(s)!;
  assert.ok(!/[\ud800-\udbff]…$/.test(out));
});

test("readPlan reads planId, planHash and action target/value/unit", () => {
  const p = readPlan({
    plan: {
      planId: "p1",
      planHash: "sha256:x",
      actions: [{ actionId: "a1", type: "set_joint", target: "_1", value: 0.5, unit: "rad", reason: "r" }],
    },
  });
  assert.equal(p.planId, "p1");
  assert.equal(p.planHash, "sha256:x");
  assert.deepEqual(p.actions, [{ actionId: "a1", target: "_1", value: 0.5, unit: "rad", reason: "r" }]);
});

test("readPlan: malformed actions drop details, never throw", () => {
  const p = readPlan({
    plan: { planId: 7, actions: [null, 5, "x", { target: "_1", value: "0.5", unit: "rad" }, { value: NaN }, { target: "_2", value: Infinity, unit: 3 }] },
  });
  assert.equal(p.planId, undefined);
  assert.equal(p.actions.length, 2);
  assert.equal(p.actions[0].value, undefined); // string value dropped
  assert.equal(p.actions[1].value, undefined); // non-finite dropped
  assert.equal(readPlan({ plan: { actions: "nope" } }).actions.length, 0);
  assert.equal(readPlan({ plan: [] }).actions.length, 0);
});

test("readPlan caps the action list", () => {
  const actions = Array.from({ length: MAX_LIST + 5 }, (_, i) => ({ target: `_${i}`, value: 1, unit: "rad" }));
  const p = readPlan({ plan: { actions } });
  assert.equal(p.actions.length, MAX_LIST);
  assert.equal(p.extraActions, 5);
});

test("readValidator reads outcome, version, errors with null plan-level fields", () => {
  const v = readValidator({
    outcome: "fail",
    validatorVersion: "0.1.0",
    errors: [
      { ruleId: "UNKNOWN_JOINT", actionId: "a1", field: "target", message: "m" },
      { ruleId: "MAX_ACTIONS_EXCEEDED", actionId: null, field: null, message: "too many" },
    ],
  });
  assert.equal(v.outcome, "fail");
  assert.equal(v.validatorVersion, "0.1.0");
  assert.equal(v.errors.length, 2);
  assert.equal(v.errors[1].actionId, null);
});

test("readValidator: wrong-typed errors and items", () => {
  assert.deepEqual(readValidator({ outcome: "fail", errors: "boom" }).errors, []);
  assert.deepEqual(readValidator({ errors: [null, 1, {}, { ruleId: 5, message: {} }] }).errors, []);
  assert.equal(readValidator({ outcome: 1, validatorVersion: {} }).outcome, undefined);
  const many = readValidator({ errors: Array.from({ length: 30 }, () => ({ ruleId: "R" })) });
  assert.equal(many.errors.length, MAX_LIST);
  assert.equal(many.extraErrors, 10);
});

test("readCritic reads reason, rules and hashes from payload.response", () => {
  const c = readCritic({
    outcome: "reject",
    planHash: "sha256:a",
    response: { reason: "why", triggeredRules: ["joint_limit_exceeded"], planHash: "sha256:b" },
  });
  assert.equal(c.outcome, "reject");
  assert.equal(c.reason, "why");
  assert.deepEqual(c.triggeredRules, ["joint_limit_exceeded"]);
  assert.equal(c.planHash, "sha256:a");
  assert.equal(c.responsePlanHash, "sha256:b");
});

test("readCritic ignores reason and rules placed at the top level", () => {
  const c = readCritic({ outcome: "reject", reason: "top", triggeredRules: ["x"] });
  assert.equal(c.reason, undefined);
  assert.deepEqual(c.triggeredRules, []);
});

test("readCritic reads the error kind and HTTP status", () => {
  const c = readCritic({ outcome: "error", error: { kind: "http_status", httpStatus: 500, detail: "d" } });
  assert.deepEqual(c.error, { kind: "http_status", httpStatus: 500, detail: "d" });
  assert.equal(readCritic({ outcome: "error", error: "x" }).error, undefined);
  assert.equal(readCritic({ error: { httpStatus: "500", kind: 3 } }).error, undefined);
});

test("readCritic: wrong-typed response and rules, capped list", () => {
  assert.deepEqual(readCritic({ response: [1] }).triggeredRules, []);
  assert.deepEqual(readCritic({ response: { triggeredRules: "a" } }).triggeredRules, []);
  assert.deepEqual(readCritic({ response: { triggeredRules: [1, null, "ok"] } }).triggeredRules, ["ok"]);
  const c = readCritic({ response: { triggeredRules: Array.from({ length: 30 }, (_, i) => `r${i}`) } });
  assert.equal(c.triggeredRules.length, MAX_LIST);
  assert.equal(c.extraRules, 10);
});

test("readExecution: dryRun is strictly boolean", () => {
  assert.equal(readExecution({ dryRun: true }).dryRun, true);
  assert.equal(readExecution({ dryRun: false }).dryRun, false);
  for (const v of ["true", 1, null, {}]) assert.equal(readExecution({ dryRun: v }).dryRun, undefined);
  assert.equal(readExecution({ dryRun: true, summary: "s" }).summary, "s");
  assert.equal(readExecution({ dryRun: true, error: "e" }).error, "e");
  assert.equal(readExecution({ error: { a: 1 } }).error, undefined);
});

test("readRaw caps at MAX_RAW and handles odd values", () => {
  const out = readRaw({ blob: "y".repeat(5000) })!;
  assert.equal(out.length, MAX_RAW + 1); // MAX_RAW characters plus the ellipsis
  assert.ok(out.endsWith("…"));
  assert.equal(readRaw(null), undefined);
  assert.equal(readRaw(undefined), undefined);
  assert.equal(readRaw(() => 1), undefined);
  assert.equal(readRaw(BigInt(10)), undefined); // JSON.stringify throws on BigInt
  assert.equal(readRaw("hi"), '"hi"');
});

test("HTML-like text is returned verbatim as a plain string", () => {
  assert.equal(readSource({ source: "<script>alert(1)</script>" }), "<script>alert(1)</script>");
});

test("readPlanFailure reads kind and detail of the planner failure shape", () => {
  assert.deepEqual(
    readPlanFailure({ outcome: "error", error: { kind: "unsupported_command", detail: "no such action" } }),
    { kind: "unsupported_command", detail: "no such action" },
  );
  assert.equal(readPlanFailure({ outcome: "error", error: { kind: "x".repeat(900) } })?.kind?.length, MAX_STRING + 1);
  assert.equal(readPlanFailure({ outcome: "error", error: { detail: "y".repeat(5000) } })?.detail?.length, MAX_STRING + 1);
});

test("readPlanFailure is undefined unless outcome is exactly \"error\"", () => {
  for (const p of [null, undefined, [], 5, "error", {}, { plan: {} }, { outcome: "ERROR" }, { outcome: true }, { outcome: "ok" }]) {
    assert.equal(readPlanFailure(p), undefined);
  }
});

test("readPlanFailure leaves out missing or wrong-typed fields and never throws", () => {
  for (const error of [undefined, null, "boom", 5, [1], { kind: 5, detail: { x: 1 } }, { kind: "", detail: "  " }]) {
    assert.deepEqual(readPlanFailure({ outcome: "error", error }), { kind: undefined, detail: undefined });
  }
});

test("readPlan falls back to planId and planHash beside plan", () => {
  const top = readPlan({ plan: { actions: [] }, planId: "p1", planHash: "sha256:x" });
  assert.equal(top.planId, "p1");
  assert.equal(top.planHash, "sha256:x");
  const nested = readPlan({ plan: { planId: "in", planHash: "h-in" }, planId: "out", planHash: "h-out" });
  assert.equal(nested.planId, "in");
  assert.equal(nested.planHash, "h-in");
  assert.equal(readPlan({ plan: {}, planId: 7 }).planId, undefined);
});
