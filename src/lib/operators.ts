// Parser for OPERATORS, a server-only variable mapping allowed sign-in emails to display names:
//   OPERATORS="abhijit@example.com=Abhijit,garvit@example.com=Garvit"
// Strict and fail closed: one bad entry, an empty entry or a repeated email rejects the WHOLE
// variable. Emails compare case-insensitively. The email never leaves the server; only the display
// name is stored and shown. Pure: no logging, no env access.

const EMAIL = /^[^\s@=,]+@[^\s@=,]+\.[^\s@=,]+$/;
const NAME = /^[\p{L}\p{N} .'-]{1,40}$/u;
const HAS_ALNUM = /[\p{L}\p{N}]/u;

export type OperatorMap = ReadonlyMap<string, string>;
export type ParsedOperators = { ok: true; byEmail: OperatorMap } | { ok: false };

export function parseOperators(raw: string | undefined): ParsedOperators {
  if (typeof raw !== "string" || raw.trim() === "") return { ok: false };
  const byEmail = new Map<string, string>();
  for (const entry of raw.split(",")) {
    const parts = entry.split("=");
    if (parts.length !== 2) return { ok: false };
    const email = parts[0].trim().toLowerCase();
    const name = parts[1].trim();
    if (!EMAIL.test(email)) return { ok: false };
    if (!NAME.test(name) || !HAS_ALNUM.test(name)) return { ok: false };
    if (byEmail.has(email)) return { ok: false };
    byEmail.set(email, name);
  }
  return { ok: true, byEmail };
}

// Display name for an allowlisted email, otherwise undefined.
export function lookupOperator(byEmail: OperatorMap, email: unknown): string | undefined {
  if (typeof email !== "string") return undefined;
  return byEmail.get(email.trim().toLowerCase());
}
