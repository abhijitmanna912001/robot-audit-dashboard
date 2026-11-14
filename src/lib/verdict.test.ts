import { test } from "node:test";
import assert from "node:assert/strict";
import { bindUnits, stageDetails, verdictFacts, verdictStamp } from "./verdict.ts";

const NB = "\u00A0";
const H = "sha256:" + "ab".repeat(32);
let n = 0;
const ev = (eventType: unknown, payload: unknown) => ({
  eventType,
  payload,
  createdAt: new Date(Date.UTC(2026, 10, 14, 10, 0, (n += 2))).toISOString(),
});
const plan = () => ev("plan_generated", { plan: { planId: "p1", planHash: H, actions: [{ actionId: "a1", target: "_1", value: 1, unit: "rad" }] } });
const vPass = () => ev("validator_result", { outcome: "pass", planId: "p1", validatorVersion: "1.0", errors: [] });
const vFail = () => ev("validator_result", { outcome: "fail", errors: [{ ruleId: "R1", actionId: "a1" }, { ruleId: "R2" }] });
const cApprove = () => ev("critic_result", { outcome: "approve", response: { triggeredRules: [] } });
const cReject = () => ev("critic_result", { outcome: "reject", response: { triggeredRules: ["X"] } });
const cErr = (kind: unknown) => ev("critic_result", { outcome: "error", error: { kind } });
const done = (dryRun: unknown) => ev("execution_completed", { dryRun });

test("verdictStamp", () => {
  assert.equal(verdictStamp("completed", [plan(), vPass(), cApprove(), done(true)]), "APPROVED · DRY RUN");
  assert.equal(verdictStamp("completed", [plan(), vPass(), cApprove(), done(false)]), "COMPLETED");
  assert.equal(verdictStamp("rejected", [plan(), vFail()]), "REJECTED");
  assert.equal(verdictStamp("failed", [ev("execution_failed", { dryRun: true })]), "FAILED");
  assert.equal(verdictStamp("executing", [ev("execution_started", { dryRun: true })]), "RUNNING · DRY RUN");
  assert.equal(verdictStamp("received", []), "WAITING");
  assert.equal(verdictStamp(undefined, null), "WAITING");
});

test("verdictFacts: derived pieces only", () => {
  assert.deepEqual(verdictFacts([plan(), vPass(), cApprove()]), [`2${NB}of${NB}2${NB}checks passed`, `0${NB}rules triggered`, `4${NB}s elapsed`]);
  assert.deepEqual(verdictFacts([plan(), vFail()]), [`0${NB}of${NB}1${NB}checks passed`, `2${NB}validator errors`, `2${NB}s elapsed`]);
  assert.deepEqual(verdictFacts([plan()]), []);
  assert.deepEqual(verdictFacts([]), []);
});

test("verdictFacts: a critic without a triggeredRules array shows no rule count", () => {
  const out = verdictFacts([ev("critic_result", { outcome: "approve" })]);
  assert.deepEqual(out, [`1${NB}of${NB}1${NB}checks passed`]);
});

test("stageDetails", () => {
  const d = stageDetails([plan(), vPass(), cApprove(), done(true)]);
  assert.deepEqual(d, { planned: "1 action", validator: "v1.0 · 1 action checked", critic: "0 rules triggered", execution: "nothing sent to a robot" });
  assert.equal(stageDetails([plan(), vFail()]).validator, "2 errors");
  assert.equal(stageDetails([plan(), vPass(), cErr("timeout")]).critic, "Critic timed out");
  assert.equal(stageDetails([ev("plan_generated", { outcome: "error", error: { kind: "unsupported_command" } })]).planned, "Not a supported command");
  assert.equal(stageDetails([plan(), vPass(), cReject()]).critic, "1 rule triggered");
});

test("malformed input never throws", () => {
  const bad = [null, undefined, 5, "x", {}, [null, 5, "x", { eventType: 5 }, { eventType: "critic_result", payload: "z" }, { eventType: "validator_result", payload: [] }]];
  for (const b of bad) {
    assert.ok(Array.isArray(verdictFacts(b)));
    assert.equal(typeof verdictStamp("x", b), "string");
    assert.equal(typeof stageDetails(b), "object");
  }
  assert.deepEqual(stageDetails([ev("critic_result", { outcome: "error", error: { kind: 5 } })]), {});
});

test("bindUnits: numbers stay with their units", () => {
  assert.equal(bindUnits("5 s elapsed"), `5${NB}s elapsed`);
  assert.equal(bindUnits("2 min 5 s elapsed"), `2${NB}min${NB}5${NB}s elapsed`);
  assert.equal(bindUnits("1 h 3 min"), `1${NB}h${NB}3${NB}min`);
  assert.equal(bindUnits("no numbers here"), "no numbers here");
  assert.equal(bindUnits(""), "");
});

test("stageDetails: dry run wording and no repeated lines", () => {
  const dryDone = stageDetails([plan(), vPass(), cApprove(), done(true)]);
  assert.equal(dryDone.execution, "nothing sent to a robot");
  assert.equal(stageDetails([plan(), vPass(), cApprove(), done(false)]).execution, "executed");
  // Started (running) dry run: the state word is "running", so "dry run" is new information.
  assert.equal(stageDetails([plan(), vPass(), cApprove(), ev("execution_started", { dryRun: true })]).execution, "dry run");
  // A failed dry run: "failed" then "dry run".
  assert.equal(stageDetails([plan(), vPass(), cApprove(), ev("execution_failed", { dryRun: true })]).execution, "dry run");
  // No detail ever equals the state word shown above it.
  const words = ["passed", "failed", "error", "not reached", "dry run", "running"];
  for (const set of [[plan(), vPass(), cApprove(), done(true)], [plan(), vFail()], [plan(), vPass(), cErr("timeout")]]) {
    for (const d of Object.values(stageDetails(set))) assert.ok(!words.includes((d as string).toLowerCase()));
  }
});
