import { test } from "node:test";
import assert from "node:assert/strict";
import { elapsedLabel, elapsedMs, formatElapsed } from "./elapsed.ts";

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

test("elapsedMs: first to last, any order, needs two valid events", () => {
  assert.equal(elapsedMs([at(0), at(41)]), 41_000);
  assert.equal(elapsedMs([at(41), at(0), at(10)]), 41_000);
  assert.equal(elapsedMs([at(5), at(5)]), 0);
  assert.equal(elapsedMs([at(0)]), undefined);
  assert.equal(elapsedMs([]), undefined);
  assert.equal(elapsedMs([at(0), { createdAt: "not a date" }]), undefined);
  assert.equal(elapsedMs([at(0), { createdAt: 5 }, { createdAt: null }, at(3)]), 3000);
  assert.equal(elapsedMs([{}, null as never, at(1), at(2)]), 1000);
  assert.equal(elapsedMs(null as never), undefined);
  assert.equal(elapsedMs("x" as never), undefined);
});

test("elapsedLabel wording follows the status", () => {
  const ev = [at(0), at(41)];
  assert.equal(elapsedLabel("completed", ev), "Completed in 41 s");
  assert.equal(elapsedLabel("rejected", ev), "Rejected after 41 s");
  assert.equal(elapsedLabel("failed", ev), "Failed after 41 s");
  assert.equal(elapsedLabel("executing", ev), "41 s so far");
  assert.equal(elapsedLabel(undefined, [at(0), at(125)]), "2 min 5 s so far");
  assert.equal(elapsedLabel("completed", [at(0)]), undefined);
  assert.equal(elapsedLabel("completed", []), undefined);
});
