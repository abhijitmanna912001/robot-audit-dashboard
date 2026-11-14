import { test } from "node:test";
import assert from "node:assert/strict";
import { formatGap } from "./time-gap.ts";

const t = (s: number, msPart = 0) => new Date(Date.UTC(2026, 10, 14, 10, 0, s, msPart)).toISOString();

test("formatGap: ranges", () => {
  assert.equal(formatGap(t(0), t(0)), "+0 ms");
  assert.equal(formatGap(t(0), t(0, 420)), "+420 ms");
  assert.equal(formatGap(t(0), t(2, 100)), "+2.1 s");
  assert.equal(formatGap(t(0), t(9, 960)), "+10 s");
  assert.equal(formatGap(t(0), t(41)), "+41 s");
  assert.equal(formatGap(t(0), t(125)), "+2m 5s");
  assert.equal(formatGap(t(0), t(120)), "+2m");
  assert.equal(formatGap(t(0), t(3780)), "+1h 3m");
  assert.equal(formatGap(t(0), t(3600)), "+1h");
});

test("formatGap: malformed or out of order gives undefined", () => {
  for (const bad of [undefined, null, 5, {}, [], "", "not a date"]) {
    assert.equal(formatGap(bad, t(1)), undefined);
    assert.equal(formatGap(t(1), bad), undefined);
  }
  assert.equal(formatGap(t(5), t(1)), undefined);
});

test("long gaps use compact units at minute and hour boundaries", () => {
  assert.equal(formatGap(t(0), t(807)), "+13m 27s");
  assert.equal(formatGap(t(0), t(59, 999)), "+1m");
  assert.equal(formatGap(t(0), t(3599, 999)), "+1h");
});
