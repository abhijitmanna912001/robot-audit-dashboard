import { test } from "node:test";
import assert from "node:assert/strict";
import type { SupabaseClient } from "@supabase/supabase-js";
import {
  RATE_LIMIT_PER_DAY,
  RATE_LIMIT_PER_MINUTE,
  handleSubmit,
  type SessionUser,
  type SubmitDeps,
} from "./submit-command.ts";

// No real Supabase and no real session: everything is injected. The email below is a test value;
// the tests assert it never reaches a response, a log line or a database write.
const EMAIL = "op.secret@example.test";
const USER: SessionUser = { id: "11111111-1111-4111-8111-111111111111", email: EMAIL };
const OPERATORS = `${EMAIL}=Abhijit,other@example.test=Garvit`;
const NOW = new Date("2026-11-15T12:00:00.000Z");
const NEW_ID = "22222222-2222-4222-8222-222222222222";

type Calls = {
  counts: { table: string; uid: unknown; since: string }[];
  commandInserts: Record<string, unknown>[];
  eventInserts: Record<string, unknown>[];
};

type Fake = {
  minuteCount?: number | "error";
  dayCount?: number | "error";
  insertError?: boolean;
  eventError?: boolean;
};

function makeDb(f: Fake, calls: Calls): SupabaseClient {
  const db = {
    from(table: string) {
      return {
        select(_cols: string, opts?: { head?: boolean }) {
          assert.equal(opts?.head, true);
          const q: { uid?: unknown; since?: string } = {};
          const chain = {
            eq(col: string, v: unknown) {
              assert.equal(col, "submitted_by_id");
              q.uid = v;
              return chain;
            },
            gte(col: string, v: string) {
              assert.equal(col, "created_at");
              q.since = v;
              return chain;
            },
            then(resolve: (v: unknown) => void) {
              calls.counts.push({ table, uid: q.uid, since: q.since as string });
              // Minute window first, then day: tell them apart by the cutoff.
              const isMinute = NOW.getTime() - new Date(q.since as string).getTime() <= 60_000;
              const c = isMinute ? f.minuteCount : f.dayCount;
              resolve(
                c === "error"
                  ? { count: null, error: { code: "X", message: "boom" } }
                  : { count: c ?? 0, error: null },
              );
            },
          };
          return chain;
        },
        insert(row: Record<string, unknown>) {
          if (table === "commands") {
            calls.commandInserts.push(row);
            return {
              select: () => ({
                single: async () =>
                  f.insertError
                    ? { data: null, error: { code: "X", message: "nope" } }
                    : { data: { id: NEW_ID }, error: null },
              }),
            };
          }
          calls.eventInserts.push(row);
          return Promise.resolve({ error: f.eventError ? { code: "X", message: "nope" } : null });
        },
      };
    },
  };
  return db as unknown as SupabaseClient;
}

function setup(over: Partial<SubmitDeps> & Fake = {}) {
  const calls: Calls = { counts: [], commandInserts: [], eventInserts: [] };
  const logs: string[] = [];
  const deps: SubmitDeps = {
    getUser: over.getUser ?? (async () => USER),
    db: over.db ?? (() => makeDb(over, calls)),
    operatorsRaw: "operatorsRaw" in over ? over.operatorsRaw : OPERATORS,
    now: () => NOW,
  };
  return { deps, calls, logs };
}

async function call(rawBody: string, over: Partial<SubmitDeps> & Fake = {}) {
  const { deps, calls, logs } = setup(over);
  const original = console.error;
  console.error = (...a: unknown[]) => void logs.push(a.map(String).join(" "));
  try {
    const res = await handleSubmit(
      new Request("http://test/api/commands", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: rawBody,
      }),
      deps,
    );
    const text = await res.text();
    // Nothing the route returns or logs may contain the email.
    assert.ok(!text.includes(EMAIL) && !logs.join("\n").includes(EMAIL), "email leaked");
    return { res, text, calls, logs };
  } finally {
    console.error = original;
  }
}

const OK = JSON.stringify({ text: "set joint _1 to 0.25 rad" });
const nothingWritten = (c: Calls) =>
  assert.deepEqual([c.commandInserts.length, c.eventInserts.length], [0, 0]);

// --- 1. session -------------------------------------------------------------------------------

test("no session -> 401, generic body, nothing touched", async () => {
  let dbTouched = false;
  const { res, text, calls } = await call(OK, {
    getUser: async () => null,
    db: () => {
      dbTouched = true;
      return makeDb({}, { counts: [], commandInserts: [], eventInserts: [] });
    },
  });
  assert.equal(res.status, 401);
  assert.deepEqual(JSON.parse(text), { error: "Unauthorized" });
  assert.equal(dbTouched, false);
  nothingWritten(calls);
});

test("no session beats a bad body and a bad OPERATORS value", async () => {
  const { res } = await call("{not json", { getUser: async () => null, operatorsRaw: "garbage" });
  assert.equal(res.status, 401);
});

test("getUser throwing is treated as no session", async () => {
  const { res, logs } = await call(OK, {
    getUser: async () => {
      throw new Error("auth down");
    },
  });
  assert.equal(res.status, 401);
  assert.ok(logs.length > 0);
});

// --- 2/3. allowlist ---------------------------------------------------------------------------

test("email not in OPERATORS -> 403, nothing written", async () => {
  const { res, text, calls } = await call(OK, {
    getUser: async () => ({ id: USER.id, email: "stranger@example.test" }),
  });
  assert.equal(res.status, 403);
  assert.deepEqual(JSON.parse(text), { error: "Forbidden" });
  assert.ok(!text.includes("stranger"));
  nothingWritten(calls);
  assert.equal(calls.counts.length, 0);
});

test("a user without an email -> 403", async () => {
  for (const email of [undefined, null, ""]) {
    const { res } = await call(OK, { getUser: async () => ({ id: USER.id, email }) });
    assert.equal(res.status, 403);
  }
});

test("allowlist compares emails case-insensitively", async () => {
  const { res } = await call(OK, {
    getUser: async () => ({ id: USER.id, email: EMAIL.toUpperCase() }),
  });
  assert.equal(res.status, 201);
});

test("OPERATORS unset or malformed -> 500 generic, nothing written", async () => {
  for (const operatorsRaw of [undefined, "", "a@x.io", "a@x.io=A,a@x.io=B", `${EMAIL}=Bad_Name`]) {
    const { res, text, calls, logs } = await call(OK, { operatorsRaw });
    assert.equal(res.status, 500);
    assert.deepEqual(JSON.parse(text), { error: "Internal error" });
    nothingWritten(calls);
    assert.ok(!logs.join("\n").includes("Bad_Name")); // the value is never logged
  }
});

// --- 4. validation (unchanged), behind a valid operator ---------------------------------------

async function expect400(rawBody: string) {
  const { res, text, calls } = await call(rawBody);
  assert.equal(res.status, 400);
  assert.equal(typeof (JSON.parse(text) as { error: string }).error, "string");
  nothingWritten(calls);
}

test("invalid JSON -> 400", () => expect400("{not json"));
test("body is an array -> 400", () => expect400("[]"));
test("body is null -> 400", () => expect400("null"));
test("missing text -> 400", () => expect400("{}"));
test("non-string text -> 400", () => expect400('{"text":123}'));
test("empty text -> 400", () => expect400('{"text":""}'));
test("whitespace-only text -> 400", () => expect400('{"text":" \\t\\n "}'));
test("501 characters -> 400", () => expect400(JSON.stringify({ text: "a".repeat(501) })));

test("500 characters passes validation; the cap applies after trimming", async () => {
  for (const text of ["a".repeat(500), `  ${"a".repeat(500)}  `]) {
    const { res, calls } = await call(JSON.stringify({ text }));
    assert.equal(res.status, 201);
    assert.equal(calls.commandInserts[0].text, "a".repeat(500));
  }
});

// --- 5. rate limit ----------------------------------------------------------------------------

test("rate limit constants", () => {
  assert.equal(RATE_LIMIT_PER_MINUTE, 5);
  assert.equal(RATE_LIMIT_PER_DAY, 50);
});

test("minute boundary: 4 allowed, 5 -> 429", async () => {
  const four = await call(OK, { minuteCount: RATE_LIMIT_PER_MINUTE - 1 });
  assert.equal(four.res.status, 201);
  const five = await call(OK, { minuteCount: RATE_LIMIT_PER_MINUTE });
  assert.equal(five.res.status, 429);
  assert.deepEqual(JSON.parse(five.text), { error: "Too many requests" });
  nothingWritten(five.calls);
});

test("day boundary: 49 allowed, 50 -> 429", async () => {
  const ok = await call(OK, { minuteCount: 0, dayCount: RATE_LIMIT_PER_DAY - 1 });
  assert.equal(ok.res.status, 201);
  const limited = await call(OK, { minuteCount: 0, dayCount: RATE_LIMIT_PER_DAY });
  assert.equal(limited.res.status, 429);
  assert.deepEqual(JSON.parse(limited.text), { error: "Too many requests" });
  nothingWritten(limited.calls);
});

test("counts are per user id and use 1-minute and 1-day windows", async () => {
  const { calls } = await call(OK);
  assert.equal(calls.counts.length, 2);
  assert.ok(calls.counts.every((c) => c.table === "commands" && c.uid === USER.id));
  const ages = calls.counts.map((c) => NOW.getTime() - new Date(c.since).getTime()).sort((a, b) => a - b);
  assert.deepEqual(ages, [60_000, 86_400_000]);
});

test("a failed count fails closed: 500, nothing inserted", async () => {
  for (const f of [{ minuteCount: "error" as const }, { minuteCount: 0, dayCount: "error" as const }]) {
    const { res, text, calls } = await call(OK, f);
    assert.equal(res.status, 500);
    assert.deepEqual(JSON.parse(text), { error: "Internal error" });
    nothingWritten(calls);
  }
});

// --- success and the existing 500 paths -------------------------------------------------------

test("success: 201 with the id; submitter name and id written, email never", async () => {
  const { res, text, calls } = await call(OK);
  assert.equal(res.status, 201);
  assert.deepEqual(JSON.parse(text), { commandId: NEW_ID });
  assert.deepEqual(calls.commandInserts, [
    { text: "set joint _1 to 0.25 rad", submitted_by: "Abhijit", submitted_by_id: USER.id },
  ]);
  assert.equal(calls.eventInserts.length, 1);
  const ev = calls.eventInserts[0];
  assert.equal(ev.command_id, NEW_ID);
  assert.equal(ev.event_type, "command_received");
  assert.deepEqual(ev.payload, { source: "dashboard", submitted_by: "Abhijit", submitted_by_id: USER.id });
  assert.ok(!JSON.stringify(calls).includes(EMAIL));
});

test("command insert failure -> 500 without a commandId", async () => {
  const { res, text } = await call(OK, { insertError: true });
  assert.equal(res.status, 500);
  assert.deepEqual(JSON.parse(text), { error: "Internal error" });
});

test("event insert failure -> 500 WITH the commandId (saved, do not resubmit)", async () => {
  const { res, text } = await call(OK, { eventError: true });
  assert.equal(res.status, 500);
  assert.deepEqual(JSON.parse(text), { error: "Command saved but audit event failed", commandId: NEW_ID });
});

test("db() throwing -> generic 500", async () => {
  const { res, text } = await call(OK, {
    db: () => {
      throw new Error("Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");
    },
  });
  assert.equal(res.status, 500);
  assert.deepEqual(JSON.parse(text), { error: "Internal error" });
  assert.ok(!/SUPABASE|service|key/i.test(text));
});
