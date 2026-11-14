import { test } from "node:test";
import assert from "node:assert/strict";
import { elapsedLabel, elapsedMs, formatElapsed, workerWaitMs, workerWaitLabel } from "./elapsed.ts";

const at = (s: number, ms = 0) => ({ createdAt: new Date(Date.UTC(2026, 10, 14, 10, 0, s, ms)).toISOString() });

test("formatElapsed", () => {
  assert.equal(formatElapsed(0), "under 1 s");
  assert.equal(formatElapsed(499), "under 1 s");
  assert.equal(formatElapsed(999), "1 s");
  assert.equal(formatElapsed(41_000), "41 s");
  assert.equal(formatElapsed(59_400), "59 s");
  assert.equal(formatElapsed(60_000), "1 min");
  assert.equal(formatElapsed(125_000), "2 min 5 s");
  assert.equal(formatElapsed(3_599_000), "59 min 59 s");
  assert.equal(formatElapsed(3_600_000), "1 h");
  assert.equal(formatElapsed(3_780_000), "1 h 3 min");
});

test("formatElapsed rejects bad input", () => {
  for (const bad of [-1, NaN, Infinity, "5", null, undefined]) assert.equal(formatElapsed(bad as never), undefined);
});

const ev = (eventType: string, seconds: number, ms = 0) => ({ eventType, ...at(seconds, ms) });

test("elapsedMs excludes worker wait and is independent of event order", () => {
  const events = [ev("command_received", 0), ev("plan_generated", 807), ev("execution_completed", 811)];
  assert.equal(elapsedMs(events), 4000);
  assert.equal(elapsedMs([...events].reverse()), 4000);
  assert.equal(workerWaitMs(events), 807_000);
  assert.equal(workerWaitLabel(events), "Waited 13 min 27 s for the worker");
});

test("processing starts at the first plan even after retries or a planner failure", () => {
  assert.equal(elapsedMs([ev("plan_generated", 4), ev("plan_generated", 8), ev("critic_result", 10)]), 6000);
  assert.equal(elapsedMs([ev("plan_generated", 4), ev("execution_completed", 4)]), 0);
});

test("missing, malformed and reversed timestamps never invent timing", () => {
  for (const events of [[], [at(0), at(41)], [ev("command_received", 0)], [ev("plan_generated", 1)], [ev("plan_generated", 5), ev("critic_result", 2)], [ev("plan_generated", 1), { createdAt: "bad" }], null, "x"]) {
    assert.equal(elapsedMs(events as never), undefined);
  }
  assert.equal(elapsedMs([null, {}, { eventType: "plan_generated", createdAt: 5 }, ev("plan_generated", 1), ev("critic_result", 3)] as never), 2000);
  assert.equal(workerWaitLabel([ev("command_received", 0), ev("critic_result", 9)]), undefined);
  assert.equal(workerWaitLabel([ev("plan_generated", 9)]), undefined);
  assert.equal(workerWaitMs([ev("command_received", 9), ev("plan_generated", 2)]), undefined);
  assert.equal(workerWaitMs([{ eventType: "command_received", createdAt: "bad" }, ev("plan_generated", 2)]), undefined);
  assert.equal(workerWaitMs(null as never), undefined);
});

test("wait labels are shown strictly over two seconds", () => {
  assert.equal(workerWaitLabel([ev("command_received", 0), ev("plan_generated", 2)]), undefined);
  assert.equal(workerWaitLabel([ev("command_received", 0), ev("plan_generated", 2, 1)]), "Waited 2 s for the worker");
});

test("elapsedLabel wording follows status and excludes queue time", () => {
  const events = [ev("command_received", 0), ev("plan_generated", 807), ev("execution_completed", 848)];
  assert.equal(elapsedLabel("completed", events), "Completed in 41 s");
  assert.equal(elapsedLabel("rejected", events), "Rejected after 41 s");
  assert.equal(elapsedLabel("failed", events), "Failed after 41 s");
  assert.equal(elapsedLabel("executing", events), "41 s so far");
  assert.equal(elapsedLabel(undefined, [ev("plan_generated", 0), ev("critic_result", 125)]), "2 min 5 s so far");
  assert.equal(elapsedLabel("completed", [ev("command_received", 0), ev("critic_result", 125)]), undefined);
});
