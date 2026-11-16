// The one message /sign-in shows after a failed password sign-in. Pure. Never includes raw error
// text or the email address, and never depends on whether the address exists or is an operator:
// every 4xx credential failure looks the same. Rate limits and network or server failures get their
// own messages; those say nothing about the address.
export const SIGN_IN_FAILED = "Email or password is incorrect.";
export const SIGN_IN_RATE_LIMITED = "Too many sign-in attempts. Please wait a few minutes and try again.";
export const SIGN_IN_UNAVAILABLE = "Could not sign in right now. Please try again in a moment.";

export function signInMessage(error: unknown): string {
  const { code, status } = (error !== null && typeof error === "object" ? error : {}) as {
    code?: unknown;
    status?: unknown;
  };
  if (status === 429 || code === "over_request_rate_limit") return SIGN_IN_RATE_LIMITED;
  // No numeric status means the request never got an answer (network failure).
  if (typeof status !== "number" || status >= 500) return SIGN_IN_UNAVAILABLE;
  return SIGN_IN_FAILED;
}
