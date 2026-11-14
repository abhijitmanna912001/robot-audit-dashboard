import { test } from "node:test";
import assert from "node:assert/strict";
import { collapsedIds } from "./command-collapse.ts";

const rows = (n: number) => Array.from({ length: n }, (_, i) => ({ id: String(i + 1) }));

test("nothing is hidden at or under the limit", () => {
  assert.equal(collapsedIds(rows(5), null, false).size, 0);
  assert.equal(collapsedIds(rows(0), null, false).size, 0);
});

test("rows past the limit are hidden, in list order", () => {
  assert.deepEqual([...collapsedIds(rows(8), null, false)], ["6", "7", "8"]);
});

test("expanded hides nothing", () => {
  assert.equal(collapsedIds(rows(8), null, true).size, 0);
});

test("the selected row is never hidden", () => {
  assert.deepEqual([...collapsedIds(rows(8), "7", false)], ["6", "8"]);
});

test("a custom limit is respected", () => {
  assert.deepEqual([...collapsedIds(rows(4), undefined, false, 2)], ["3", "4"]);
});
