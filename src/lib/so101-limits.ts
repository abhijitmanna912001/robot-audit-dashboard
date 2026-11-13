// SO-101 joint position limits, loaded from the one shared JSON file.
// Source of the numbers: docs/robot-capabilities.md section 6 (inclusive bounds).
// Validated once at load: a malformed file throws, so tests and the build fail loudly.
import raw from "../../shared/so101-limits.json" with { type: "json" };

export type JointLimit = { readonly min: number; readonly max: number };

function load(): { unit: string; limits: ReadonlyMap<string, JointLimit>; maxActions: number } {
  const { unit, joints, maxActions } = raw as { unit: unknown; joints: unknown; maxActions: unknown };
  if (typeof unit !== "string" || unit.trim() === "") {
    throw new Error("so101-limits.json: unit must be a non-empty string");
  }
  if (typeof maxActions !== "number" || !Number.isInteger(maxActions) || maxActions < 1) {
    throw new Error("so101-limits.json: maxActions must be a positive integer");
  }
  if (typeof joints !== "object" || joints === null || Array.isArray(joints)) {
    throw new Error("so101-limits.json: joints must be an object");
  }
  const limits = new Map<string, JointLimit>();
  for (const [name, value] of Object.entries(joints)) {
    const { min, max } = (value ?? {}) as { min?: unknown; max?: unknown };
    if (
      typeof min !== "number" || typeof max !== "number" ||
      !Number.isFinite(min) || !Number.isFinite(max) || min > max
    ) {
      throw new Error(`so101-limits.json: bad limits for joint ${name}`);
    }
    limits.set(name, Object.freeze({ min, max }));
  }
  if (limits.size === 0) throw new Error("so101-limits.json: no joints");
  return { unit, limits, maxActions };
}

const loaded = load();

export const JOINT_UNIT: string = loaded.unit;
// Maximum actions per plan. worker/robot_profile.py holds the same number;
// worker/tests/test_safety_validator.py fails if the two differ.
export const MAX_ACTIONS: number = loaded.maxActions;
// A Map, not an object: "constructor" or "__proto__" must not look like joints.
export const JOINT_LIMITS: ReadonlyMap<string, JointLimit> = loaded.limits;
