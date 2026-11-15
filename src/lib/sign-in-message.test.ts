import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SIGN_IN_EMAIL_RATE_LIMITED,
  SIGN_IN_FAILED,
  SIGN_IN_SENT,
  signInMessage,
} from "./sign-in-message.ts";

const EMAIL = "someone@example.test";

test("over_email_send_rate_limit (429) shows the hour message, without raw text or the email", () => {
  const msg = signInMessage({
    code: "over_email_send_rate_limit",
    status: 429,
    message: `email rate limit exceeded for ${EMAIL}`,
  });
  assert.equal(msg, "Too many sign-in emails were sent recently. Please try again in about an hour.");
  assert.equal(msg, SIGN_IN_EMAIL_RATE_LIMITED);
  assert.ok(!msg.includes(EMAIL) && !msg.includes("exceeded"));
});

test("every other error keeps the generic messages and never leaks raw text or the email", () => {
  const raw = `boom for ${EMAIL}`;
  const cases: [unknown, string][] = [
    [{ code: "over_request_rate_limit", status: 429, message: raw }, SIGN_IN_FAILED], // other 429
    [{ status: 429, message: raw }, SIGN_IN_FAILED],
    [{ code: "unexpected_failure", status: 500, message: raw }, SIGN_IN_FAILED],
    [{ message: raw }, SIGN_IN_FAILED], // network failure, no status
    [{ code: "otp_disabled", status: 422, message: raw }, SIGN_IN_SENT], // unknown address looks like success
    [{ code: "email_address_invalid", status: 400, message: raw }, SIGN_IN_SENT],
    [null, SIGN_IN_SENT],
    [undefined, SIGN_IN_SENT],
  ];
  for (const [err, want] of cases) {
    const got = signInMessage(err);
    assert.equal(got, want, JSON.stringify(err));
    assert.ok(!got.includes(EMAIL) && !got.includes("boom"));
  }
});
