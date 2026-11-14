import { test } from "node:test";
import assert from "node:assert/strict";
import { filterCommands, statusCounts } from "./command-filter.ts";

const c = (id: string, status: string, text = "set joint _1 to 0.25 rad") => ({ id, status: status as never, text });
const list = [c("1", "completed"), c("2", "rejected"), c("3", "failed"), c("4", "received"), c("5", "completed", "[smoke test] x")];
const ids = (xs: { id: string }[]) => xs.map((x) => x.id);

test("all hides test commands unless shown", () => {
  assert.deepEqual(ids(filterCommands(list, { tab: "all", showTest: false })), ["1", "2", "3", "4"]);
  assert.deepEqual(ids(filterCommands(list, { tab: "all", showTest: true })), ["1", "2", "3", "4", "5"]);
});

test("tabs filter by status", () => {
  assert.deepEqual(ids(filterCommands(list, { tab: "attention", showTest: false })), ["2", "3"]);
  assert.deepEqual(ids(filterCommands(list, { tab: "completed", showTest: false })), ["1"]);
  assert.deepEqual(ids(filterCommands(list, { tab: "completed", showTest: true })), ["1", "5"]);
});

test("the selected command always stays visible", () => {
  assert.deepEqual(ids(filterCommands(list, { tab: "attention", showTest: false, selectedId: "4" })), ["2", "3", "4"]);
  assert.deepEqual(ids(filterCommands(list, { tab: "all", showTest: false, selectedId: "5" })), ["1", "2", "3", "4", "5"]);
});

test("empty input and unknown selected id", () => {
  assert.deepEqual(filterCommands([], { tab: "all", showTest: true, selectedId: "x" }), []);
});

test("statusCounts counts every status, ignores unknown ones", () => {
  const counts = statusCounts([...list, c("6", "bogus")]);
  assert.equal(counts.completed, 2);
  assert.equal(counts.rejected, 1);
  assert.equal(counts.executing, 0);
  assert.equal(Object.values(counts).reduce((a, b) => a + b, 0), 5);
  assert.equal(statusCounts([]).received, 0);
});
