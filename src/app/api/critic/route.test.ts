import { test } from "node:test";
import assert from "node:assert/strict";
import { POST } from "./route.ts";
import { readFileSync } from "node:fs";
import { computePlanHash } from "../../../lib/plan-hash.ts";
import { JOINT_LIMITS, JOINT_UNIT, MAX_ACTIONS } from "../../../lib/so101-limits.ts";

const SECRET = "test-secret-not-real";
process.env.CRITIC_SHARED_SECRET = SECRET;

const act = (n: number, target: string, value: unknown = 1, extra: Record<string, unknown> = {}) => ({
  actionId: `a${n}`, type: "set_joint", target, value, unit: "rad", reason: "x", ...extra,
});
const goodActions = () => [act(1, "_1"), act(2, "_2")];

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
  const wrong = computePlanHash([act(9, "_3")]);
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
  const actionsJson = '[{"actionId":"a1","type":"set_joint","target":"_1","value":1e999,"unit":"rad","reason":"x"}]';
  const res = await call(bodyText(actionsJson, "sha256:" + "0".repeat(64)));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["plan_hash_error"]);
  assert.equal("planHash" in body, false);
});

test("null value gives null_value_not_allowed", async () => {
  const actions = [act(1, "_1", null)];
  const res = await call(bodyText(JSON.stringify(actions), computePlanHash(actions)));
  const body = (await res.json()) as Verdict;
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(body.triggeredRules, ["null_value_not_allowed"]); // not malformed_action too
  assert.equal(body.planHash, computePlanHash(actions));
});

test("nested null is caught", async () => {
  const actions = [act(1, "_1", 1, { extra: { deep: [1, { n: null }] } })];
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
      name: "joint limit",
      text: bodyText(JSON.stringify([act(1, "_1", 9)]), computePlanHash([act(1, "_1", 9)])),
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

// ---- plan rules (mirror worker/safety_validator.py) ------------------------

async function verdictFor(actions: unknown[]) {
  const res = await call(bodyText(JSON.stringify(actions), computePlanHash(actions)));
  return { res, body: (await res.json()) as Verdict };
}
const rulesOf = (b: Verdict) => [...b.triggeredRules].sort();

const JOINTS = [...JOINT_LIMITS.keys()];

test("every joint at its exact min is approved", async () => {
  const actions = JOINTS.map((j, i) => act(i + 1, j, JOINT_LIMITS.get(j)!.min));
  const { res, body } = await verdictFor(actions);
  assert.equal(res.status, 200);
  assert.equal(body.verdict, "approve", body.reason);
  assert.deepEqual(body.triggeredRules, []);
});

test("every joint at its exact max is approved", async () => {
  const actions = JOINTS.map((j, i) => act(i + 1, j, JOINT_LIMITS.get(j)!.max));
  const { body } = await verdictFor(actions);
  assert.equal(body.verdict, "approve", body.reason);
});

test("just outside either limit of any joint gives joint_limit_exceeded", async () => {
  for (const j of JOINTS) {
    const { min, max } = JOINT_LIMITS.get(j)!;
    for (const value of [max + 1e-6, min - 1e-6]) {
      const { body } = await verdictFor([act(1, j, value)]);
      assert.equal(body.verdict, "reject", `${j} ${value}`);
      assert.deepEqual(body.triggeredRules, ["joint_limit_exceeded"], `${j} ${value}`);
    }
  }
});

test("unsupported action types give unsupported_action_type", async () => {
  for (const type of ["move_forward", "stop", "jump"]) {
    const { body } = await verdictFor([act(1, "_1", 1, { type })]);
    assert.deepEqual(body.triggeredRules, ["unsupported_action_type"], type);
  }
});

test("unknown joints give unknown_joint (including names on Object.prototype)", async () => {
  for (const target of ["joint_a", "_7", "_1 ", "constructor", "toString", "__proto__"]) {
    const { body } = await verdictFor([act(1, target)]);
    assert.equal(body.verdict, "reject", target);
    assert.deepEqual(body.triggeredRules, ["unknown_joint"], target);
  }
});

test("wrong unit gives unit_mismatch and no limit error", async () => {
  for (const unit of ["deg", "RAD", "rad "]) {
    const { body } = await verdictFor([act(1, "_1", 99, { unit })]);
    assert.deepEqual(body.triggeredRules, ["unit_mismatch"], unit);
  }
});

test("max actions: 11 is rejected with max_actions_exceeded, 10 is not", async () => {
  // Only 6 joints exist and a joint may be used once, so these plans also trip
  // duplicate_joint_target. The cap rule itself is what is checked here.
  const plan = (n: number) => Array.from({ length: n }, (_, i) => act(i + 1, JOINTS[i % JOINTS.length]));
  const eleven = await verdictFor(plan(11));
  assert.ok(eleven.body.triggeredRules.includes("max_actions_exceeded"));
  const ten = await verdictFor(plan(10));
  assert.ok(!ten.body.triggeredRules.includes("max_actions_exceeded"));
});

test("action cap comes from shared/so101-limits.json and is the agreed 10", () => {
  const shared = JSON.parse(
    readFileSync(new URL("../../../../shared/so101-limits.json", import.meta.url), "utf8"),
  ) as { maxActions: unknown };
  assert.equal(shared.maxActions, 10);
  assert.equal(MAX_ACTIONS, shared.maxActions);
  // The route must not keep its own copy of the number.
  const source = readFileSync(new URL("./route.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /MAX_ACTIONS\s*=/);
});

test("two actions on the same joint give duplicate_joint_target", async () => {
  const { body } = await verdictFor([act(1, "_4", 0), act(2, "_4", 0.5)]);
  assert.deepEqual(body.triggeredRules, ["duplicate_joint_target"]);
});

test("a repeated actionId gives duplicate_action_id", async () => {
  const { body } = await verdictFor([act(1, "_1"), { ...act(1, "_2") }]);
  assert.deepEqual(body.triggeredRules, ["duplicate_action_id"]);
});

test("extra fields give unexpected_field", async () => {
  const { body } = await verdictFor([act(1, "_1", 1, { speed: 3 })]);
  assert.deepEqual(body.triggeredRules, ["unexpected_field"]);
});

test("whitespace-only strings and missing/wrong-typed fields give malformed_action", async () => {
  const bad: Record<string, unknown>[] = [
    { actionId: "   " },
    { reason: " \t" },
    { target: "  " },
    { unit: " " },
    { type: " " },
    { value: "1" },
    { value: true },
  ];
  for (const override of bad) {
    const { body } = await verdictFor([act(1, "_1", 1, override)]);
    assert.equal(body.verdict, "reject", JSON.stringify(override));
    assert.ok(body.triggeredRules.includes("malformed_action"), JSON.stringify(override));
  }
  for (const missing of ["unit", "target", "value", "reason", "actionId", "type"]) {
    const a: Record<string, unknown> = act(1, "_1");
    delete a[missing];
    const { body } = await verdictFor([a]);
    assert.ok(body.triggeredRules.includes("malformed_action"), `missing ${missing}`);
  }
  const { body } = await verdictFor(["not an object", 5]);
  assert.deepEqual(body.triggeredRules, ["malformed_action"]);
});

test("empty plan is still rejected", async () => {
  const { res, body } = await verdictFor([]);
  assert.equal(res.status, 200);
  assert.deepEqual(body.triggeredRules, ["empty_actions"]);
});

test("all violations are reported together", async () => {
  const { body } = await verdictFor([
    act(1, "_1", 99),
    act(1, "_2", 0, { type: "stop" }), // repeated actionId, unsupported type
    act(3, "nope", 0, { unit: "deg", extra: 1 }),
    act(4, "_1", 0), // second action on _1
  ]);
  assert.equal(body.verdict, "reject");
  assert.deepEqual(rulesOf(body), [
    "duplicate_action_id",
    "duplicate_joint_target",
    "joint_limit_exceeded",
    "unexpected_field",
    "unit_mismatch",
    "unknown_joint",
    "unsupported_action_type",
  ].sort());
});

test("limits JSON matches the section 6 table in docs/robot-capabilities.md", () => {
  const doc = readFileSync(new URL("../../../../docs/robot-capabilities.md", import.meta.url), "utf8");
  assert.equal(JOINT_UNIT, "rad");
  const rows = doc.split("\n").filter((l) => /^\| `_\d` \| revolute \|/.test(l));
  assert.equal(rows.length, JOINT_LIMITS.size);
  for (const row of rows) {
    const c = row.split("|").map((x) => x.trim());
    const name = c[1].replaceAll("`", "");
    assert.deepEqual(JOINT_LIMITS.get(name), { min: Number(c[3]), max: Number(c[4]) }, name);
  }
});
