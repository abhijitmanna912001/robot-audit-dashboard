import { test } from "node:test";
import assert from "node:assert/strict";
import { POST } from "./route.ts";
import { computePlanHash } from "../../../lib/plan-hash.ts";

const SECRET = "test-secret-not-real";
process.env.CRITIC_SHARED_SECRET = SECRET;

const goodActions = () => [
  { actionId: "a1", type: "set_joint", value: 1, reason: "x" },
  { actionId: "a2", type: "stop", reason: "y" },
];

function call(rawBody: string, secret: string | null = SECRET) {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (secret !== null) headers["x-critic-secret"] = secret;
  return POST(new Request("http://test/api/critic", { method: "POST", headers, body: rawBody }));
}

// Build the body as text so cases like 1e999 reach the route exactly as JSON.
function bodyText(actionsJson: string, planHash?: string | number) {
  const parts = ['"commandId":"c1"', '"planId":"p1"', `"actions":${actionsJson}`];
  if (planHash !== undefined) parts.push(`"planHash":${JSON.stringify(planHash)}`);
  return `{${parts.join(",")}}`;
}

type Verdict = { verdict: string; triggeredRules: string[]; planHash?: string; reason: string };

test("correct hash approves and echoes the recomputed hash", async () => {
  const actions = goodActions();
  const hash = computePlanHash(actions);
  const res = await call(bodyText(JSON.stringify(actions), hash));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "approve");
  assert.equal(body.planHash, hash);
  assert.deepEqual(body.triggeredRules, []);
});

test("wrong hash gives plan_hash_mismatch and returns the critic's own hash", async () => {
  const actions = goodActions();
  const real = computePlanHash(actions);
  const wrong = computePlanHash([{ actionId: "other", type: "stop", reason: "z" }]);
  const res = await call(bodyText(JSON.stringify(actions), wrong));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["plan_hash_mismatch"]);
  assert.equal(body.planHash, real); // never the supplied value
  assert.notEqual(body.planHash, wrong);
});

test("missing planHash gives 400 missing_fields", async () => {
  const res = await call(bodyText(JSON.stringify(goodActions())));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 400);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["missing_fields"]);
});

test("non-string planHash gives 400 missing_fields", async () => {
  const res = await call(bodyText(JSON.stringify(goodActions()), 123));
  assert.equal(res.status, 400);
  assert.deepEqual(((await res.json()) as Verdict).triggeredRules, ["missing_fields"]);
});

test("hashing error (value 1e999) gives plan_hash_error and no planHash", async () => {
  const actionsJson = '[{"actionId":"a1","type":"set_joint","value":1e999,"reason":"x"}]';
  const res = await call(bodyText(actionsJson, "sha256:" + "0".repeat(64)));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["plan_hash_error"]);
  assert.equal("planHash" in body, false);
});

test("null value gives null_value_not_allowed", async () => {
  const actions = [{ actionId: "a1", type: "set_joint", value: null, reason: "x" }];
  const res = await call(bodyText(JSON.stringify(actions), computePlanHash(actions)));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["null_value_not_allowed"]); // not malformed_action too
  assert.equal(body.planHash, computePlanHash(actions));
});

test("nested null is caught", async () => {
  const actions = [{ actionId: "a1", type: "stop", reason: "x", extra: { deep: [1, { n: null }] } }];
  const res = await call(bodyText(JSON.stringify(actions), computePlanHash(actions)));
  const body = (await res.json()) as Verdict;
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["null_value_not_allowed"]);
});

test("a null action is caught", async () => {
  const actions = [null];
  const res = await call(bodyText(JSON.stringify(actions), computePlanHash(actions)));
  const body = (await res.json()) as Verdict;
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["null_value_not_allowed"]);
});

test("missing or wrong secret gives 401", async () => {
  const text = bodyText(JSON.stringify(goodActions()), computePlanHash(goodActions()));
  for (const secret of [null, "wrong"]) {
    const res = await call(text, secret);
    const body = (await res.json()) as Verdict;
    assert.equal(res.status, 401);
    assert.equal(body.verdict, "reject");
    assert.deepEqual(body.triggeredRules, ["unauthorized"]);
  }
});

test("every reject path returns verdict reject", async () => {
  const good = goodActions();
  const goodHash = computePlanHash(good);
  const cases: { name: string; text: string; secret?: string | null; status: number }[] = [
    { name: "unauthorized", text: bodyText(JSON.stringify(good), goodHash), secret: null, status: 401 },
    { name: "invalid json", text: "{not json", status: 400 },
    { name: "missing planHash", text: bodyText(JSON.stringify(good)), status: 400 },
    { name: "hash error", text: bodyText('[{"value":1e999}]', goodHash), status: 200 },
    { name: "hash mismatch", text: bodyText(JSON.stringify(good), "sha256:bad"), status: 200 },
    { name: "empty actions", text: bodyText("[]", computePlanHash([])), status: 200 },
    { name: "null value", text: bodyText('[{"value":null}]', computePlanHash([{ value: null }])), status: 200 },
    {
      name: "existing rule (joint range)",
      text: bodyText(
        JSON.stringify([{ actionId: "a1", type: "set_joint", value: 9, reason: "x" }]),
        computePlanHash([{ actionId: "a1", type: "set_joint", value: 9, reason: "x" }]),
      ),
      status: 200,
    },
  ];
  for (const c of cases) {
    const res = await call(c.text, c.secret === undefined ? SECRET : c.secret);
    const body = (await res.json()) as Verdict;
    assert.equal(res.status, c.status, c.name);
    assert.equal(body.verdict, "reject", c.name);
    assert.ok(body.triggeredRules.length > 0, c.name);
  }
});
