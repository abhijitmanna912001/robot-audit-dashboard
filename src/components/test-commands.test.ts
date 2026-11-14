import assert from "node:assert/strict";
import { test } from "node:test";
import { isTestCommand } from "./test-commands.ts";

test("matches commands that start with [smoke test]", () => {
  assert.equal(isTestCommand("[smoke test]"), true);
  assert.equal(isTestCommand("[smoke test] set joint _1 to 0.1 rad"), true);
  assert.equal(isTestCommand("  [smoke test] padded"), true);
});

test("matches text made only of the letter a", () => {
  assert.equal(isTestCommand("a"), true);
  assert.equal(isTestCommand("aaaa"), true);
  assert.equal(isTestCommand("  aaa  "), true);
});

test("never matches anything else", () => {
  for (const text of [
    "",
    "   ",
    "ab",
    "ba",
    "a a",
    "A",
    "AAA",
    "aaab",
    "[smoke tests]",
    "[Smoke Test]",
    "hello [smoke test]",
    "smoke test",
    "set joint _1 to 0.1 rad",
    "aa\naa",
  ]) {
    assert.equal(isTestCommand(text), false, JSON.stringify(text));
  }
});

test("non-string input is never a test command", () => {
  assert.equal(isTestCommand(undefined), false);
  assert.equal(isTestCommand(null), false);
  assert.equal(isTestCommand(42), false);
});
