import { test } from "node:test";
import assert from "node:assert/strict";
import { canonicalizeActions, computePlanHash } from "./plan-hash.ts";

// Reference array, parsed from text so 1.0 is written exactly as in the contract.
const REFERENCE_JSON =
  '[{"actionId":"a1","type":"set_joint","target":"joint_a","value":1.0,"unit":"rad","reason":"x"},{"actionId":"a2","type":"set_joint","target":"joint_b","value":0.30000000000000004,"unit":"rad","reason":"y"}]';

const reference = (): Record<string, unknown>[] => JSON.parse(REFERENCE_JSON);

const EXPECTED_CANONICAL =
  '[{"actionId":"a1","reason":"x","target":"joint_a","type":"set_joint","unit":"rad","value":1},{"actionId":"a2","reason":"y","target":"joint_b","type":"set_joint","unit":"rad","value":0.30000000000000004}]';

// Computed outside this code: printf '%s' "$EXPECTED_CANONICAL" | shasum -a 256
const EXPECTED_HASH =
  "sha256:e97478a64c5d373d46d3abf1d1e544643578eb8107159fe5abe96a183464c684";

test("canonical JSON matches the expected RFC 8785 string", () => {
  assert.equal(canonicalizeActions(reference()), EXPECTED_CANONICAL);
});

test("hash has the sha256: prefix and 64 lowercase hex characters", () => {
  assert.match(computePlanHash(reference()), /^sha256:[0-9a-f]{64}$/);
});

test("hash equals the independently computed reference value", () => {
  assert.equal(computePlanHash(reference()), EXPECTED_HASH);
});

test("key order in the input does not change the hash", () => {
  const reordered = reference().map((action) =>
    Object.fromEntries(Object.entries(action).reverse()),
  );
  assert.notDeepEqual(Object.keys(reordered[0]), Object.keys(reference()[0]));
  assert.equal(computePlanHash(reordered), EXPECTED_HASH);
});

test("key order is sorted recursively in nested objects", () => {
  const a = [{ x: { b: 1, a: 2 }, y: 1 }];
  const b = [{ y: 1, x: { a: 2, b: 1 } }];
  assert.equal(computePlanHash(a), computePlanHash(b));
});

test("array order changes the hash", () => {
  const swapped = reference().reverse();
  assert.notEqual(computePlanHash(swapped), EXPECTED_HASH);
});

test("changing any value changes the hash", () => {
  const changes: Record<string, unknown> = {
    actionId: "a9",
    type: "stop",
    target: "joint_z",
    value: 2,
    unit: "deg",
    reason: "changed",
  };
  for (let i = 0; i < 2; i++) {
    for (const [field, newValue] of Object.entries(changes)) {
      const actions = reference();
      actions[i][field] = newValue;
      assert.notEqual(
        computePlanHash(actions),
        EXPECTED_HASH,
        `changing actions[${i}].${field} must change the hash`,
      );
    }
  }
});

test("a tiny float change changes the hash", () => {
  const actions = reference();
  actions[1].value = 0.3;
  assert.notEqual(computePlanHash(actions), EXPECTED_HASH);
});

test("1.0 and 1 hash the same (number formatting)", () => {
  const one = reference();
  one[0].value = 1;
  assert.equal(computePlanHash(one), EXPECTED_HASH);
});
