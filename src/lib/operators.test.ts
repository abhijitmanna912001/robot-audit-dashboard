import { test } from "node:test";
import assert from "node:assert/strict";
import { lookupOperator, parseOperators } from "./operators.ts";

function ok(raw: string) {
  const r = parseOperators(raw);
  assert.equal(r.ok, true, raw);
  return r.ok ? r.byEmail : new Map<string, string>();
}

test("valid map, trimmed and lowercased emails", () => {
  const m = ok(" Abhijit@Example.com = Abhijit , garvit@example.com=Garvit ");
  assert.equal(m.size, 2);
  assert.equal(m.get("abhijit@example.com"), "Abhijit");
  assert.equal(m.get("garvit@example.com"), "Garvit");
});

test("lookup is case-insensitive and tolerates junk input", () => {
  const m = ok("abhijit@example.com=Abhijit");
  assert.equal(lookupOperator(m, "ABHIJIT@example.COM"), "Abhijit");
  assert.equal(lookupOperator(m, " abhijit@example.com "), "Abhijit");
  assert.equal(lookupOperator(m, "other@example.com"), undefined);
  for (const bad of [undefined, null, 7, {}, ""]) assert.equal(lookupOperator(m, bad), undefined);
});

test("display names: allowed characters", () => {
  const m = ok("a@x.io=Mary-Jane O'Neil Jr.,b@x.io=Op 2,c@x.io=Zoë,d@x.io=" + "n".repeat(40));
  assert.equal(m.get("a@x.io"), "Mary-Jane O'Neil Jr.");
  assert.equal(m.get("c@x.io"), "Zoë");
  assert.equal(m.get("d@x.io")?.length, 40);
});

test("unset, empty and blank are rejected (fail closed)", () => {
  for (const raw of [undefined, "", "   "]) assert.deepEqual(parseOperators(raw), { ok: false });
});

test("any malformed entry rejects the whole variable", () => {
  const bad = [
    "a@x.io=A,",
    ",a@x.io=A",
    "a@x.io=A,,b@x.io=B",
    "a@x.io",
    "a@x.io=",
    "=Name",
    "a@x.io=A=B",
    "a@x.io=" + "n".repeat(41),
    "a@x.io=A_B",
    "a@x.io=<b>",
    "a@x.io=...",
    "a@x.io=-",
    "a@x.io=   ",
    "not-an-email=A",
    "a@b=A",
    "a b@x.io=A",
    "a@x.io=A,bad",
  ];
  for (const raw of bad) assert.deepEqual(parseOperators(raw), { ok: false }, raw);
});

test("a repeated email (any case) rejects the whole variable", () => {
  assert.deepEqual(parseOperators("a@x.io=A,A@X.io=B"), { ok: false });
  assert.deepEqual(parseOperators("a@x.io=A,a@x.io=A"), { ok: false });
});
