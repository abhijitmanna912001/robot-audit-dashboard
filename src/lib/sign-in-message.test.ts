import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SIGN_IN_FAILED,
  SIGN_IN_RATE_LIMITED,
  SIGN_IN_UNAVAILABLE,
  signInMessage,
} from "./sign-in-message.ts";

const EMAIL = "someone@example.test";

test("wrong password and unknown email (4xx) give the identical generic message", () => {
  const wrong = signInMessage({ code: "invalid_credentials", status: 400, message: `bad password for ${EMAIL}` });
  const unknown = signInMessage({ code: "invalid_credentials", status: 400, message: "Invalid login credentials" });
  assert.equal(wrong, "Email or password is incorrect.");
  assert.equal(wrong, unknown);
  assert.equal(wrong, SIGN_IN_FAILED);
  assert.equal(signInMessage({ code: "user_not_found", status: 422, message: EMAIL }), SIGN_IN_FAILED);
  assert.ok(!wrong.includes(EMAIL));
});

test("rate limit (429 or over_request_rate_limit) shows the wait message", () => {
  const want = "Too many sign-in attempts. Please wait a few minutes and try again.";
  assert.equal(SIGN_IN_RATE_LIMITED, want);
  assert.equal(signInMessage({ code: "over_request_rate_limit", status: 429, message: `slow down ${EMAIL}` }), want);
  assert.equal(signInMessage({ status: 429, message: "x" }), want);
  assert.equal(signInMessage({ code: "over_request_rate_limit", message: "x" }), want);
});

test("network and 5xx failures show the try-again message, never raw text", () => {
  const want = "Could not sign in right now. Please try again in a moment.";
  assert.equal(SIGN_IN_UNAVAILABLE, want);
  const raw = `boom for ${EMAIL}`;
  const cases: unknown[] = [
    { message: raw }, // network failure, no status
    new TypeError(raw),
    { code: "unexpected_failure", status: 500, message: raw },
    { status: 503, message: raw },
    "string error",
    null,
    undefined,
  ];
  for (const err of cases) {
    const got = signInMessage(err);
    assert.equal(got, want);
    assert.ok(!got.includes(EMAIL) && !got.includes("boom"));
  }
});
