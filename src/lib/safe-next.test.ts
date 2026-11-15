import { test } from "node:test";
import assert from "node:assert/strict";
import { safeNextPath } from "./safe-next.ts";

test("same-origin paths pass", () => {
  for (const p of ["/", "/?command=abc", "/sign-in", "/a/b?x=1#y"]) assert.equal(safeNextPath(p), p);
});

test("everything else becomes /", () => {
  for (const p of [
    undefined, null, "", "x", "https://evil.test", "//evil.test", "/\\evil.test", "http:evil",
    "/a\\b", "/\n/evil.test", "javascript:alert(1)", "///evil.test",
  ]) {
    assert.equal(safeNextPath(p), "/", String(p));
  }
});
