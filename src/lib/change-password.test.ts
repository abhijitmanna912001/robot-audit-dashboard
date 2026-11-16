import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PW_CURRENT_REQUIRED,
  PW_MISMATCH,
  PW_SAME,
  PW_TOO_SHORT,
  validatePasswordChange,
} from "./change-password.ts";

const OLD = "old-temporary-pass";
const NEW = "a-much-longer-passphrase";

test("valid change passes", () => {
  assert.equal(validatePasswordChange({ current: OLD, next: NEW, confirm: NEW }), null);
});

test("new password shorter than 12 characters is rejected", () => {
  assert.equal(validatePasswordChange({ current: OLD, next: "elevenchars", confirm: "elevenchars" }), PW_TOO_SHORT);
  assert.equal(validatePasswordChange({ current: OLD, next: "twelve-chars", confirm: "twelve-chars" }), null);
});

test("confirmation mismatch is rejected", () => {
  assert.equal(validatePasswordChange({ current: OLD, next: NEW, confirm: NEW + "x" }), PW_MISMATCH);
});

test("new password equal to the current one is rejected", () => {
  const same = "same-password-123";
  assert.equal(validatePasswordChange({ current: same, next: same, confirm: same }), PW_SAME);
});

test("empty current password is rejected", () => {
  assert.equal(validatePasswordChange({ current: "", next: NEW, confirm: NEW }), PW_CURRENT_REQUIRED);
});
