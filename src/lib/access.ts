// What the page may show about the viewer: signed out, signed in but not allowlisted, or an
// operator (with the display name only; the email is never passed to the UI). Display only: the
// route re-checks everything server-side. A malformed OPERATORS value means nobody is an operator.
import { lookupOperator, parseOperators } from "./operators.ts";

export type Access =
  | { state: "signed_out" }
  | { state: "not_allowed" }
  | { state: "operator"; name: string };

export function resolveAccess(
  user: { email?: string | null } | null | undefined,
  operatorsRaw: string | undefined,
): Access {
  if (!user) return { state: "signed_out" };
  const ops = parseOperators(operatorsRaw);
  if (!ops.ok) return { state: "not_allowed" };
  const name = lookupOperator(ops.byEmail, user.email);
  return name === undefined ? { state: "not_allowed" } : { state: "operator", name };
}
