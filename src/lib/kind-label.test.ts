import { test } from "node:test";
import assert from "node:assert/strict";
import { isKnownKind, kindLabel } from "./kind-label.ts";

test("kindLabel: known kinds", () => {
  assert.equal(kindLabel("unsupported_command"), "Not a supported command");
  assert.equal(kindLabel("planner_error"), "Planner error");
  assert.equal(kindLabel("http_status"), "Critic returned an HTTP error");
  assert.equal(kindLabel("timeout"), "Critic timed out");
  assert.equal(kindLabel("invalid_body"), "Critic returned an invalid response");
  assert.equal(kindLabel("  timeout \n"), "Critic timed out");
});

test("kindLabel: unknown kinds pass through, capped", () => {
  assert.equal(kindLabel("planner_errr"), "planner_errr");
  assert.equal(kindLabel("<b>kind</b>"), "<b>kind</b>");
  assert.equal(kindLabel("a\n b"), "a b");
  const long = kindLabel("z".repeat(900)) as string;
  assert.ok(long.length <= 41 && long.endsWith("…")); // payload.text: 40 characters plus an ellipsis
  assert.ok(long.startsWith("zzzz"));
});

test("kindLabel: malformed input gives undefined and never throws", () => {
  for (const bad of [undefined, null, 5, true, {}, [], ["timeout"], "", "   ", "\n"]) {
    assert.equal(kindLabel(bad), undefined);
  }
  // Prototype keys are not known kinds.
  assert.equal(isKnownKind("toString"), false);
  assert.equal(kindLabel("toString"), "toString");
  assert.equal(isKnownKind("timeout"), true);
  assert.equal(isKnownKind(5), false);
});
