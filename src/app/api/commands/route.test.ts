import { test } from "node:test";
import assert from "node:assert/strict";
import { POST } from "./route.ts";

// Validation runs before any DB call. With the env unset, a request that
// passes validation must fail closed with a generic 500 (no DB is contacted).
delete process.env.NEXT_PUBLIC_SUPABASE_URL;
delete process.env.SUPABASE_SERVICE_ROLE_KEY;

function call(rawBody: string) {
  return POST(
    new Request("http://test/api/commands", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: rawBody,
    }),
  );
}

async function expect400(rawBody: string) {
  const res = await call(rawBody);
  assert.equal(res.status, 400);
  const json = (await res.json()) as { error: string };
  assert.equal(typeof json.error, "string");
}

test("invalid JSON -> 400", () => expect400("{not json"));
test("body is an array -> 400", () => expect400("[]"));
test("body is null -> 400", () => expect400("null"));
test("missing text -> 400", () => expect400("{}"));
test("non-string text -> 400", () => expect400('{"text":123}'));
test("empty text -> 400", () => expect400('{"text":""}'));
test("whitespace-only text -> 400", () => expect400('{"text":" \\t\\n "}'));
test("501 characters -> 400", () =>
  expect400(JSON.stringify({ text: "a".repeat(501) })));

test("500 characters passes validation (then fails closed on missing env)", async () => {
  const res = await call(JSON.stringify({ text: "a".repeat(500) }));
  assert.equal(res.status, 500);
  assert.deepEqual(await res.json(), { error: "Internal error" });
});

test("cap is applied after trimming", async () => {
  const res = await call(JSON.stringify({ text: `  ${"a".repeat(500)}  ` }));
  assert.equal(res.status, 500); // not 400: trimmed length is exactly 500
});

test("500 response body is generic and leaks nothing", async () => {
  const res = await call('{"text":"move forward"}');
  assert.equal(res.status, 500);
  const text = await res.text();
  assert.deepEqual(JSON.parse(text), { error: "Internal error" });
  assert.ok(!/SUPABASE|service|key/i.test(text));
});
