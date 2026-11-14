import { test } from "node:test";
import assert from "node:assert/strict";
import { bannerTone, derivePipeline, type StageState } from "./pipeline.ts";
import { summarize } from "./summary.ts";

const e = (eventType: string, payload: unknown) => ({ eventType, payload });

const received = e("command_received", { source: "dashboard" });
const planOk = e("plan_generated", { plan: { planId: "p", planHash: "h", actions: [] } });
const planErr = (kind = "unsupported_command") =>
  e("plan_generated", { outcome: "error", error: { kind, detail: "d" } });
const vPass = e("validator_result", { outcome: "pass", errors: [] });
const vFail = e("validator_result", { outcome: "fail", errors: [{ ruleId: "JOINT_LIMIT_EXCEEDED" }] });
const cApprove = e("critic_result", { outcome: "approve" });
const cReject = e("critic_result", { outcome: "reject", response: { triggeredRules: ["x"] } });
const cError = e("critic_result", { outcome: "error", error: { kind: "timeout" } });
const xStart = (dryRun?: unknown) => e("execution_started", { dryRun });
const xDone = (dryRun?: unknown) => e("execution_completed", { dryRun, summary: "ok" });
const xFail = (dryRun?: unknown) => e("execution_failed", { dryRun, error: "boom" });

const states = (events: ReturnType<typeof e>[]): StageState[] =>
  derivePipeline(events).map((s) => s.state);

test("stage order is fixed", () => {
  assert.deepEqual(
    derivePipeline([]).map((s) => s.stage),
    ["planned", "validator", "critic", "execution"],
  );
});

test("no events: nothing reached", () => {
  assert.deepEqual(states([]), ["not_reached", "not_reached", "not_reached", "not_reached"]);
  assert.deepEqual(states([received]), ["not_reached", "not_reached", "not_reached", "not_reached"]);
});

test("plan without error is passed", () => {
  assert.deepEqual(states([received, planOk]), ["passed", "not_reached", "not_reached", "not_reached"]);
});

test("planner failure: Planned error, the rest not reached, even with later stage events", () => {
  assert.deepEqual(states([received, planErr()]), ["error", "not_reached", "not_reached", "not_reached"]);
  assert.deepEqual(states([planErr("planner_error"), vPass, cApprove, xDone(true)]), [
    "error",
    "not_reached",
    "not_reached",
    "not_reached",
  ]);
});

test("a later successful plan clears an earlier planner failure", () => {
  assert.deepEqual(states([planErr(), planOk]), ["passed", "not_reached", "not_reached", "not_reached"]);
  assert.deepEqual(states([planOk, planErr()])[0], "error");
});

test("validator rejection: Validator failed, Execution not reached", () => {
  assert.deepEqual(states([planOk, vFail]), ["passed", "failed", "not_reached", "not_reached"]);
});

test("critic rejection: Critic failed, Execution not reached", () => {
  assert.deepEqual(states([planOk, vPass, cReject]), ["passed", "passed", "failed", "not_reached"]);
});

test("critic error: Critic error, Execution not reached", () => {
  assert.deepEqual(states([planOk, vPass, cError]), ["passed", "passed", "error", "not_reached"]);
});

test("validator fail then critic error reports each stage honestly", () => {
  assert.deepEqual(states([planOk, vFail, cError]), ["passed", "failed", "error", "not_reached"]);
});

test("validator fail and reject are sticky", () => {
  assert.equal(states([planOk, vFail, vPass])[1], "failed");
  assert.equal(states([planOk, vPass, cReject, cApprove])[2], "failed");
});

test("critic: latest recognised outcome wins between approve and error", () => {
  assert.equal(states([planOk, vPass, cError, cApprove])[2], "passed");
  assert.equal(states([planOk, vPass, cApprove, cError])[2], "error");
});

test("approved, waiting for execution", () => {
  assert.deepEqual(states([planOk, vPass, cApprove]), ["passed", "passed", "passed", "not_reached"]);
});

test("execution running: started only", () => {
  assert.deepEqual(states([planOk, vPass, cApprove, xStart(true)]), ["passed", "passed", "passed", "running"]);
});

test("execution completed: dryRun true is dry_run, anything else is passed", () => {
  const base = [planOk, vPass, cApprove];
  assert.equal(states([...base, xStart(true), xDone(true)])[3], "dry_run");
  assert.equal(states([...base, xStart(false), xDone(false)])[3], "passed");
  assert.equal(states([...base, xDone()])[3], "passed");
  assert.equal(states([...base, xDone("true")])[3], "passed"); // a string is not boolean true
  assert.equal(states([...base, xDone(1)])[3], "passed");
});

test("execution failed", () => {
  const base = [planOk, vPass, cApprove];
  assert.equal(states([...base, xStart(true), xFail(true)])[3], "failed");
  assert.equal(states([...base, xFail()])[3], "failed");
});

test("latest terminal execution event wins", () => {
  const base = [planOk, vPass, cApprove];
  assert.equal(states([...base, xFail(), xDone(true)])[3], "dry_run");
  assert.equal(states([...base, xDone(true), xFail()])[3], "failed");
});

test("execution after a rejection stays not reached", () => {
  assert.equal(states([planOk, vFail, xDone(true)])[3], "not_reached");
  assert.equal(states([planOk, vPass, cReject, xDone(false)])[3], "not_reached");
  assert.equal(states([planOk, vPass, cError, xDone(false)])[3], "not_reached");
});

test("unreadable outcomes count as not reached", () => {
  const bad = [
    e("validator_result", { outcome: 5 }),
    e("validator_result", { outcome: "maybe" }),
    e("critic_result", { outcome: null }),
    e("critic_result", { response: [1, 2] }),
  ];
  assert.deepEqual(states([planOk, ...bad]), ["passed", "not_reached", "not_reached", "not_reached"]);
});

test("malformed events and payloads never throw", () => {
  const nasty = [
    null,
    undefined,
    5,
    "text",
    [],
    {},
    { eventType: 7, payload: 1 },
    e("plan_generated", null),
    e("plan_generated", [1, 2]),
    e("plan_generated", "boom"),
    e("validator_result", null),
    e("validator_result", { outcome: "x".repeat(900), errors: "boom" }),
    e("critic_result", "str"),
    e("critic_result", { outcome: "reject", response: [1], error: "x" }),
    e("execution_started", null),
    e("execution_completed", [1, 2, 3]),
    e("execution_failed", { dryRun: {} }),
    e("something_new", { x: 1 }),
  ] as unknown as ReturnType<typeof e>[];
  assert.doesNotThrow(() => derivePipeline(nasty));
  assert.doesNotThrow(() => bannerTone("received", nasty));
  assert.equal(derivePipeline(nasty).length, 4);
  assert.doesNotThrow(() => derivePipeline(null as unknown as ReturnType<typeof e>[]));
  assert.deepEqual(
    derivePipeline(undefined as unknown as ReturnType<typeof e>[]).map((s) => s.state),
    ["not_reached", "not_reached", "not_reached", "not_reached"],
  );
});

test("plan_generated with a non-error payload is passed even when empty", () => {
  assert.equal(states([e("plan_generated", {})])[0], "passed");
  assert.equal(states([e("plan_generated", { outcome: "ok" })])[0], "passed");
});

// bannerTone must agree with the summary text for the same events.
const cases: {
  name: string;
  status: string;
  events: ReturnType<typeof e>[];
  tone: ReturnType<typeof bannerTone>;
  text: string;
}[] = [
  { name: "waiting", status: "received", events: [received], tone: "waiting", text: "Waiting for the worker" },
  { name: "validator rejection", status: "rejected", events: [planOk, vFail], tone: "blocked", text: "Rejected by validator: JOINT_LIMIT_EXCEEDED" },
  { name: "critic rejection", status: "rejected", events: [planOk, vPass, cReject], tone: "blocked", text: "Rejected by critic: x" },
  { name: "validator fail + critic error", status: "rejected", events: [planOk, vFail, cError], tone: "blocked", text: "Rejected by validator: JOINT_LIMIT_EXCEEDED" },
  { name: "critic error", status: "rejected", events: [planOk, vPass, cError], tone: "broke", text: "Critic unavailable: nothing executed" },
  { name: "unsupported command", status: "failed", events: [received, planErr()], tone: "broke", text: "Command not understood: nothing executed" },
  { name: "planner error", status: "failed", events: [received, planErr("planner_error")], tone: "broke", text: "Planning failed: nothing executed" },
  { name: "approved, waiting", status: "approved", events: [planOk, vPass, cApprove], tone: "waiting", text: "Approved, waiting for execution" },
  { name: "executing", status: "executing", events: [planOk, vPass, cApprove, xStart(false)], tone: "running", text: "Executing" },
  { name: "executing dry run", status: "executing", events: [planOk, vPass, cApprove, xStart(true)], tone: "running", text: "Executing (dry run)" },
  { name: "dry run completed", status: "completed", events: [planOk, vPass, cApprove, xStart(true), xDone(true)], tone: "success", text: "Dry run completed" },
  { name: "execution completed", status: "completed", events: [planOk, vPass, cApprove, xStart(false), xDone(false)], tone: "success", text: "Execution completed" },
  { name: "execution failed", status: "failed", events: [planOk, vPass, cApprove, xFail(false)], tone: "broke", text: "Execution failed" },
  { name: "dry run failed", status: "failed", events: [planOk, vPass, cApprove, xFail(true)], tone: "broke", text: "Dry run failed" },
];

for (const c of cases) {
  test(`banner tone and summary agree: ${c.name}`, () => {
    assert.equal(summarize(c.status, c.events), c.text);
    assert.equal(bannerTone(c.status, c.events), c.tone);
  });
}

test("banner tone falls back on the command status when events explain nothing", () => {
  assert.equal(bannerTone("rejected", []), "blocked");
  assert.equal(bannerTone("failed", []), "broke");
  assert.equal(bannerTone("completed", []), "success");
  assert.equal(bannerTone("executing", []), "running");
  assert.equal(bannerTone("approved", []), "waiting");
  assert.equal(bannerTone("received", []), "waiting");
  assert.equal(bannerTone(undefined, []), "waiting");
  assert.equal(bannerTone("???", []), "waiting");
});
