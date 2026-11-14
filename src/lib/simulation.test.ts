import { test } from "node:test";
import assert from "node:assert/strict";
import { eventTitle } from "./event-summary.ts";
import { bindUnits, stageDetails, verdictFacts, verdictStamp } from "./verdict.ts";
import { summarize } from "./summary.ts";
import {
  JOINTS,
  executionMode,
  executionOutcome,
  formatDelta,
  formatRad,
  formatTolerance,
  isSimulation,
  latestSimulation,
  movedJoints,
  readSimulation,
  readSnapshot,
  restoreCheck,
  simulationKindLabel,
  telemetryRows,
  type Snapshot,
} from "./simulation.ts";

const AT = "2026-11-14T10:00:01Z";
const snap = (joints: Record<string, unknown>, readAt: unknown = AT) => ({ readAt, joints });
const S = (joints: Partial<Record<string, number>>): Snapshot => readSnapshot(snap(joints)) as Snapshot;
const SIM = { dryRun: false, mode: "simulation" };

test("isSimulation / executionMode are strict", () => {
  assert.equal(isSimulation(SIM), true);
  for (const bad of [
    { mode: "simulation" },
    { mode: "simulation", dryRun: "false" },
    { mode: "simulation", dryRun: 0 },
    { mode: "simulation", dryRun: null },
    { mode: "simulation", dryRun: true },
    { mode: "Simulation", dryRun: false },
    { mode: " simulation", dryRun: false },
    { mode: "live", dryRun: false },
    { dryRun: false },
    null,
    undefined,
    "simulation",
    [],
    42,
  ]) {
    assert.equal(isSimulation(bad), false, JSON.stringify(bad));
  }
  assert.equal(executionMode({ dryRun: true }), "dry_run");
  assert.equal(executionMode({ dryRun: true, mode: "simulation" }), "dry_run");
  assert.equal(executionMode(SIM), "simulation");
  for (const other of [{ dryRun: false }, {}, { dryRun: "true" }, { dryRun: false, mode: "live" }, null, 7]) {
    assert.equal(executionMode(other), "unknown");
  }
});

test("readSnapshot keeps only valid UTC snapshots and _1.._6", () => {
  assert.deepEqual(readSnapshot(snap({ _1: 0.5, _6: -1 })), { readAt: AT, joints: { _1: 0.5, _6: -1 } });
  assert.deepEqual(readSnapshot(snap({ _1: 0, extra: 5, _7: 1 }))?.joints, { _1: 0 }); // extra keys ignored
  assert.deepEqual(readSnapshot(snap({ _1: 0 }, "2026-11-14T10:00:01.250Z"))?.readAt, "2026-11-14T10:00:01.250Z");
  for (const readAt of [
    "2026-11-14T10:00:01", // no Z
    "2026-11-14T10:00:01+00:00",
    "2026-11-14T10:00:01+02:00",
    "2026-11-14 10:00:01Z",
    "2026-02-31T10:00:01Z", // rolls over
    "2026-11-14T25:00:01Z",
    "yesterday",
    "",
    1731578401,
    null,
  ]) {
    assert.equal(readSnapshot(snap({ _1: 0 }, readAt)), undefined, String(readAt));
  }
  assert.equal(readSnapshot({ joints: { _1: 0 } }), undefined); // readAt missing
  // A present joint that is not a finite number invalidates the snapshot.
  for (const bad of [Number.NaN, Infinity, -Infinity, "0.5", null, undefined, {}, [], true]) {
    assert.equal(readSnapshot(snap({ _1: 0, _2: bad })), undefined, String(bad));
  }
  // No usable joint at all.
  assert.equal(readSnapshot(snap({})), undefined);
  assert.equal(readSnapshot(snap({ x: 1 })), undefined);
  assert.equal(readSnapshot({ readAt: AT }), undefined);
  assert.equal(readSnapshot({ readAt: AT, joints: [1, 2] }), undefined);
  for (const junk of [null, undefined, 5, "x", [], true]) assert.equal(readSnapshot(junk), undefined);
});

test("readSimulation: only for exact simulation payloads, capped, never throws", () => {
  assert.equal(readSimulation({ dryRun: true }), undefined);
  assert.equal(readSimulation({ dryRun: false }), undefined);
  assert.equal(readSimulation({ mode: "simulation" }), undefined);
  assert.equal(readSimulation(null), undefined);
  const d = readSimulation({
    ...SIM,
    summary: "ok",
    telemetry: { start: snap({ _1: 0 }), target: snap({ _1: 0.5 }), restored: snap({ _1: 0.001 }), extra: 1 },
    toleranceRad: 0.01,
    stop: "confirmed",
    error: { kind: "timeout", detail: "d" },
    lastTelemetry: snap({ _2: 1 }),
  });
  assert.equal(d?.mode, "simulation");
  assert.equal(d?.toleranceRad, 0.01);
  assert.equal(d?.stop, "confirmed");
  assert.deepEqual(d?.error, { kind: "timeout", detail: "d" });
  assert.deepEqual(d?.start?.joints, { _1: 0 });
  assert.deepEqual(d?.lastTelemetry?.joints, { _2: 1 });
  // Bad stop, tolerance and telemetry are left out.
  const bad = readSimulation({ ...SIM, stop: "maybe", toleranceRad: "0.01", telemetry: "x", error: "boom", lastTelemetry: 3 });
  assert.equal(bad?.stop, undefined);
  assert.equal(bad?.toleranceRad, undefined);
  assert.equal(bad?.start, undefined);
  assert.equal(bad?.error, undefined);
  assert.equal(bad?.lastTelemetry, undefined);
  assert.equal(readSimulation({ ...SIM, toleranceRad: Infinity })?.toleranceRad, undefined);
  // Strings are capped at 200, control characters are neutralised.
  const long = readSimulation({ ...SIM, summary: "x".repeat(900), error: { kind: "k", detail: "a‮b" + "y".repeat(900) } });
  assert.ok((long?.summary?.length ?? 0) <= 201);
  assert.ok((long?.error?.detail?.length ?? 0) <= 201);
  assert.ok(!long?.error?.detail?.includes("‮"));
  // Hostile shapes never throw.
  for (const p of [{ ...SIM, telemetry: null }, { ...SIM, telemetry: [1] }, { ...SIM, error: [] }, { ...SIM, get x() { return 1; } }]) {
    assert.doesNotThrow(() => readSimulation(p));
  }
});

test("restoreCheck: claims restored only with both snapshots and a finite positive tolerance", () => {
  const start = S({ _1: 0, _2: 1 });
  const ok = S({ _1: 0.005, _2: 1 });
  assert.deepEqual(restoreCheck(start, ok, 0.01), { status: "restored", maxDelta: 0.005, toleranceRad: 0.01 });
  // Boundary: exactly the tolerance is within it.
  assert.equal(restoreCheck(S({ _1: 0 }), S({ _1: 0.5 }), 0.5).status, "restored");
  const out = restoreCheck(start, S({ _1: 0.05, _2: 1 }), 0.01);
  assert.equal(out.status, "outside");
  // One joint outside is enough, even when the rest match.
  assert.equal(restoreCheck(S({ _1: 0, _2: 0, _3: 0 }), S({ _1: 0, _2: 0, _3: 0.5 }), 0.1).status, "outside");
  // Missing pieces are never restored.
  assert.deepEqual(restoreCheck(undefined, ok, 0.01), { status: "not_verified", reason: "no_start" });
  assert.deepEqual(restoreCheck(start, undefined, 0.01), { status: "not_verified", reason: "no_restored" });
  for (const tol of [undefined, 0, -0.01, Number.NaN, Infinity, "0.01", null, {}]) {
    assert.deepEqual(restoreCheck(start, ok, tol), { status: "not_verified", reason: "no_tolerance" }, String(tol));
  }
  // Only joints in both are compared; none in common is not verified.
  assert.equal(restoreCheck(S({ _1: 0, _2: 0 }), S({ _1: 0, _3: 9 }), 0.01).status, "restored");
  assert.deepEqual(restoreCheck(S({ _1: 0 }), S({ _2: 0 }), 0.01), { status: "not_verified", reason: "no_common_joints" });
});

test("movedJoints", () => {
  const a = S({ _1: 0, _2: 0, _3: 0 });
  const b = S({ _1: 0.5, _2: 0.005, _3: 0 });
  assert.deepEqual([...movedJoints(a, b, 0.01)], ["_1"]);
  // No usable tolerance: any difference counts.
  assert.deepEqual([...movedJoints(a, b)].sort(), ["_1", "_2"]);
  assert.deepEqual([...movedJoints(a, b, -1)].sort(), ["_1", "_2"]);
  assert.equal(movedJoints(undefined, b, 0.01).size, 0);
  assert.equal(movedJoints(a, undefined, 0.01).size, 0);
});

test("formatters", () => {
  assert.equal(formatRad(0.5), "0.500");
  assert.equal(formatRad(-0.0004), "0.000"); // never "-0.000"
  assert.equal(formatRad(-1.23456), "-1.235");
  assert.equal(formatDelta(0.0012, 0), "+0.001");
  assert.equal(formatDelta(0, 0.5), "-0.500");
  assert.equal(formatDelta(0.0001, 0), "0.000");
  assert.equal(formatTolerance(0.01), "0.01");
  assert.equal(formatTolerance(0.050001), "0.05");
  assert.equal(formatTolerance(1), "1");
});

const ev = (eventType: string, payload: unknown) => ({ eventType, payload, createdAt: "2026-11-14T10:00:00Z" });
const tel = (restored: number, tolerance?: number) => ({
  ...SIM,
  telemetry: { start: snap({ _1: 0 }), target: snap({ _1: 0.5 }), restored: snap({ _1: restored }) },
  ...(tolerance === undefined ? {} : { toleranceRad: tolerance }),
});

test("executionOutcome: every headline, stamp, tone and fact", () => {
  const out = (type: string, payload: unknown) => executionOutcome([ev(type, payload)]);
  assert.deepEqual(out("execution_completed", tel(0.001, 0.01)), {
    headline: "Simulated move completed",
    stamp: "APPROVED · SIMULATED",
    tone: "success",
    facts: ["arm restored within 0.01 rad"],
  });
  assert.deepEqual(out("execution_completed", tel(0.08, 0.01)), {
    headline: "Simulated move completed: arm not restored",
    stamp: "SIMULATED · NOT RESTORED",
    tone: "warning",
    facts: ["largest difference from start 0.080 rad, tolerance 0.01 rad"],
  });
  const nt = out("execution_completed", tel(0.001));
  assert.equal(nt?.headline, "Simulated move completed: restore not verified");
  assert.equal(nt?.tone, "warning");
  assert.deepEqual(nt?.facts, ["restore not verified: tolerance missing or invalid"]);
  const nostart = out("execution_completed", { ...SIM, toleranceRad: 0.01 });
  assert.equal(nostart?.headline, "Simulated move completed: restore not verified");
  assert.deepEqual(nostart?.facts, ["restore not verified: start reading missing"]);
  assert.deepEqual(out("execution_started", SIM), { headline: "Executing (simulation)", stamp: "RUNNING · SIMULATED", tone: "running", facts: [] });

  const fail = (stop: unknown, kind: unknown = "overall_timeout") =>
    out("execution_failed", { ...SIM, error: { kind, detail: "no reply" }, stop });
  assert.deepEqual(fail("confirmed"), {
    headline: "Simulation failed: Simulation timed out",
    stamp: "FAILED · SIMULATED",
    tone: "broke",
    facts: ["simulation confirmed stopped", "Detail: no reply"],
  });
  const notNeeded = fail("not_needed", "session_crashed");
  assert.equal(notNeeded?.headline, "Simulation failed: Simulation session crashed");
  assert.equal(notNeeded?.tone, "broke");
  assert.deepEqual(notNeeded?.facts, ["no stop was needed", "Detail: no reply"]);
  assert.equal(fail("confirmed", "telemetry_invalid")?.headline, "Simulation failed: Telemetry invalid");
  assert.equal(fail("confirmed", null)?.headline, "Simulation failed");
  const unc = fail("unconfirmed");
  assert.equal(unc?.headline, "Simulation failed: arm state uncertain");
  assert.equal(unc?.tone, "warning");
  assert.equal(unc?.facts[0], "Manual check needed. Nothing was retried.");
  for (const stop of [undefined, "maybe", 3, null]) {
    const m = fail(stop);
    assert.equal(m?.headline, "Simulation failed: stop not recorded", String(stop));
    assert.equal(m?.tone, "warning");
    assert.ok(!m?.facts.includes("Manual check needed. Nothing was retried."));
  }
});

test("mode not recorded: warning, no claim either way; dry runs untouched", () => {
  for (const payload of [{ dryRun: false }, {}, { dryRun: "true" }, { dryRun: false, mode: "live" }, { mode: "simulation" }, { mode: "simulation", dryRun: "false" }, null, "x"]) {
    const o = executionOutcome([ev("execution_completed", payload)]);
    assert.deepEqual(o, { headline: "Execution finished: mode not recorded", stamp: "MODE NOT RECORDED", tone: "warning", facts: [] });
  }
  assert.equal(executionOutcome([ev("execution_completed", { dryRun: true })]), undefined);
  assert.equal(executionOutcome([ev("execution_failed", { dryRun: false })]), undefined);
  assert.equal(executionOutcome([ev("execution_started", {})]), undefined);
  assert.equal(executionOutcome([]), undefined);
  assert.equal(executionOutcome(null), undefined);
});

test("summarize, stamp, facts and stage details agree for simulations", () => {
  const base = [ev("plan_generated", {}), ev("validator_result", { outcome: "pass" }), ev("critic_result", { outcome: "approve", response: { triggeredRules: [] } })];
  const done = [...base, ev("execution_started", SIM), ev("execution_completed", tel(0.001, 0.01))];
  assert.equal(summarize("completed", done), "Simulated move completed");
  assert.equal(verdictStamp("completed", done), "APPROVED · SIMULATED");
  assert.ok(verdictFacts(done).includes(bindUnits("arm restored within 0.01 rad")));
  assert.equal(stageDetails(done).execution, undefined); // "simulated" is already the state word
  const failed = [...base, ev("execution_started", SIM), ev("execution_failed", { ...SIM, error: { kind: "gate_unconfirmed" }, stop: "unconfirmed" })];
  assert.equal(summarize("failed", failed), "Simulation failed: arm state uncertain");
  assert.equal(verdictStamp("failed", failed), "FAILED · ARM STATE UNCERTAIN");
  assert.equal(verdictFacts(failed)[0], "Manual check needed. Nothing was retried.");
  assert.equal(stageDetails(failed).execution, "simulation");
  const running = [...base, ev("execution_started", SIM)];
  assert.equal(summarize("executing", running), "Executing (simulation)");
  assert.equal(verdictStamp("executing", running), "RUNNING · SIMULATED");
  // A rejection still wins over stray simulation events.
  const rejected = [ev("validator_result", { outcome: "fail", errors: [{ ruleId: "R" }] }), ev("execution_completed", tel(0.001, 0.01))];
  assert.equal(verdictStamp("rejected", rejected), "REJECTED");
  assert.equal(summarize("rejected", rejected), "Rejected by validator: R");
  assert.ok(!verdictFacts(rejected).some((f) => f.includes("restored")));
});

test("eventTitle", () => {
  assert.equal(eventTitle(ev("execution_started", SIM)), "Simulation started");
  assert.equal(eventTitle(ev("execution_completed", SIM)), "Simulated move completed");
  assert.equal(eventTitle(ev("execution_failed", { ...SIM, error: { kind: "interrupted_write" } })), "Interrupted while recording");
  assert.equal(eventTitle(ev("execution_failed", { ...SIM, error: { kind: "weird_kind" } })), "Weird kind");
  // The planner/critic table is not used for simulations: "timeout" is just sentence-cased.
  assert.equal(eventTitle(ev("execution_failed", { ...SIM, error: { kind: "timeout" } })), "Timeout");
  assert.equal(eventTitle(ev("execution_failed", { ...SIM, error: "x" })), "Simulation failed");
  assert.equal(eventTitle(ev("execution_started", { dryRun: false })), "Execution started (mode not recorded)");
  assert.equal(eventTitle(ev("execution_completed", { dryRun: false })), "Execution finished (mode not recorded)");
  assert.equal(eventTitle(ev("execution_failed", { mode: "simulation" })), "Execution failed (mode not recorded)");
  assert.equal(eventTitle(ev("execution_completed", null)), "Execution finished (mode not recorded)");
  // Dry runs and other events keep the worker's message.
  assert.equal(eventTitle(ev("execution_completed", { dryRun: true })), undefined);
  assert.equal(eventTitle(ev("critic_result", {})), undefined);
  assert.equal(eventTitle(null as never), undefined);
});

test("telemetryRows", () => {
  const sim = readSimulation({
    ...SIM,
    telemetry: { start: snap({ _1: 0, _2: 0.1 }), target: snap({ _1: 0.5, _2: 0.1 }), restored: snap({ _1: 0.0004, _2: 0.1 }, "2026-11-14T10:00:05Z") },
    toleranceRad: 0.01,
  });
  const t = telemetryRows(sim);
  assert.deepEqual(t?.columns.map((c) => c.label), ["Start", "Target", "Restored"]);
  assert.equal(t?.columns[2].readAt, "2026-11-14T10:00:05Z");
  assert.equal(t?.showBack, true);
  assert.equal(t?.rows.length, JOINTS.length);
  assert.deepEqual(t?.rows[0], { joint: "_1", values: { start: "0.000", target: "0.500", restored: "0.000" }, back: "0.000", moved: true });
  assert.equal(t?.rows[1].moved, false);
  assert.deepEqual(t?.rows[2].values, {}); // a joint with no reading has no value, never 0
  assert.equal(t?.rows[2].back, undefined);
  // Only a last reading: one column, no "Back to start".
  const last = telemetryRows(readSimulation({ ...SIM, lastTelemetry: snap({ _1: 0.31 }) }));
  assert.deepEqual(last?.columns.map((c) => c.key), ["last"]);
  assert.equal(last?.showBack, false);
  assert.equal(last?.rows[0].back, undefined);
  // No snapshot, no table.
  assert.equal(telemetryRows(readSimulation({ ...SIM })), undefined);
  assert.equal(telemetryRows(undefined), undefined);
  // Start without restored: no "Back to start".
  assert.equal(telemetryRows(readSimulation({ ...SIM, telemetry: { start: snap({ _1: 0 }) } }))?.showBack, false);
});

test("latestSimulation: terminal beats started, malformed is not simulated", () => {
  assert.equal(latestSimulation([ev("execution_started", SIM)])?.mode, "simulation");
  assert.equal(latestSimulation([ev("execution_started", { dryRun: true })]), undefined);
  assert.equal(latestSimulation([ev("execution_completed", { mode: "simulation", dryRun: "false", telemetry: { start: snap({ _1: 0 }) } })]), undefined);
  assert.equal(latestSimulation(null), undefined);
  assert.equal(latestSimulation([null, 5, { eventType: "execution_completed" }]), undefined);
});

test("simulationKindLabel: known, unknown, empty and non-string kinds", () => {
  const known: Record<string, string> = {
    overall_timeout: "Simulation timed out",
    connection_failed: "Could not connect to the simulation",
    gate_unconfirmed: "Movement blocked by a safety gate",
    interrupted: "Interrupted",
    interrupted_write: "Interrupted while recording",
    session_crashed: "Simulation session crashed",
  };
  for (const [k, v] of Object.entries(known)) assert.equal(simulationKindLabel(k), v);
  assert.equal(simulationKindLabel("  overall_timeout  "), "Simulation timed out");
  assert.equal(simulationKindLabel("telemetry_invalid"), "Telemetry invalid");
  assert.equal(simulationKindLabel("SOME__ODD_Kind"), "Some odd kind");
  assert.equal(simulationKindLabel("timeout"), "Timeout"); // not the critic label
  assert.equal(simulationKindLabel("toString"), "Tostring"); // no prototype lookups
  assert.ok((simulationKindLabel("x".repeat(500)) ?? "").length <= 41); // capped
  assert.equal(simulationKindLabel("a\u202Eb"), "A b"); // invisible characters become spaces
  for (const empty of ["", "   ", "___", "\n"]) assert.equal(simulationKindLabel(empty), undefined, JSON.stringify(empty));
  for (const bad of [undefined, null, 7, {}, [], true, Symbol.iterator as unknown]) {
    assert.equal(simulationKindLabel(bad), undefined);
  }
});
