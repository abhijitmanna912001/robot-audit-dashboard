import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveAccess } from "./access.ts";

const OPS = "op@example.test=Abhijit";

test("signed out, not allowed, operator", () => {
  assert.deepEqual(resolveAccess(null, OPS), { state: "signed_out" });
  assert.deepEqual(resolveAccess(undefined, OPS), { state: "signed_out" });
  assert.deepEqual(resolveAccess({ email: "x@example.test" }, OPS), { state: "not_allowed" });
  assert.deepEqual(resolveAccess({ email: null }, OPS), { state: "not_allowed" });
  assert.deepEqual(resolveAccess({ email: "OP@example.test" }, OPS), { state: "operator", name: "Abhijit" });
});

test("malformed or unset OPERATORS: nobody is an operator, and the email is not in the result", () => {
  for (const raw of [undefined, "", "bad", "op@example.test=Bad_Name"]) {
    assert.deepEqual(resolveAccess({ email: "op@example.test" }, raw), { state: "not_allowed" });
  }
  assert.ok(!JSON.stringify(resolveAccess({ email: "op@example.test" }, OPS)).includes("op@example"));
});
