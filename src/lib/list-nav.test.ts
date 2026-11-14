import { test } from "node:test";
import assert from "node:assert/strict";
import { stepIndex } from "./list-nav.ts";

test("stepIndex: moves and wraps", () => {
  assert.equal(stepIndex(5, 0, "ArrowDown"), 1);
  assert.equal(stepIndex(5, 4, "ArrowDown"), 0);
  assert.equal(stepIndex(5, 0, "ArrowUp"), 4);
  assert.equal(stepIndex(5, 3, "ArrowUp"), 2);
  assert.equal(stepIndex(5, 2, "Home"), 0);
  assert.equal(stepIndex(5, 2, "End"), 4);
});

test("stepIndex: other keys and bad input are ignored", () => {
  assert.equal(stepIndex(5, 2, "Enter"), undefined);
  assert.equal(stepIndex(5, 2, "ArrowLeft"), undefined);
  assert.equal(stepIndex(0, 0, "ArrowDown"), undefined);
  assert.equal(stepIndex(Number.NaN, 0, "ArrowDown"), undefined);
  assert.equal(stepIndex(3, -1, "ArrowDown"), 1); // out-of-range index counts as the first row
  assert.equal(stepIndex(3, 9, "ArrowUp"), 2);
});
