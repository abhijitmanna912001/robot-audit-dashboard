// The one message /sign-in shows after requesting a link. Pure. Never includes raw error text or
// the email address. An unknown or non-allowlisted address looks exactly like success (sign-ups are
// off, so Supabase errors for them), so the form cannot be used to find out who is an operator.
export const SIGN_IN_SENT = "If this email can sign in, a link is on its way. Check your inbox.";
export const SIGN_IN_FAILED = "Could not send the link. Try again in a minute.";
export const SIGN_IN_EMAIL_RATE_LIMITED =
  "Too many sign-in emails were sent recently. Please try again in about an hour.";

export function signInMessage(error: unknown): string {
  if (error === null || error === undefined) return SIGN_IN_SENT;
  const { code, status } = error as { code?: unknown; status?: unknown };
  if (code === "over_email_send_rate_limit") return SIGN_IN_EMAIL_RATE_LIMITED;
  // Other rate limits and server or network failures say nothing about the address.
  if (status === 429 || typeof status !== "number" || status >= 500) return SIGN_IN_FAILED;
  return SIGN_IN_SENT;
}
