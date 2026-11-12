import { createHash } from "node:crypto";
import canonicalize from "canonicalize";

// Plan hash contract: docs/schema-draft.md, "Plan hash contract".
// SHA-256 of the RFC 8785 canonical JSON of the `actions` array only,
// UTF-8 encoded, lowercase hex, prefixed with "sha256:".
// Server-only (uses node:crypto). Not wired into any route yet.

/** RFC 8785 canonical JSON string of the actions array. */
export function canonicalizeActions(actions: readonly unknown[]): string {
  const canonical = canonicalize(actions);
  if (canonical === undefined) {
    throw new Error("actions could not be canonicalized");
  }
  return canonical;
}

/** "sha256:" + lowercase hex SHA-256 of the canonical actions JSON. */
export function computePlanHash(actions: readonly unknown[]): string {
  const digest = createHash("sha256")
    .update(canonicalizeActions(actions), "utf8")
    .digest("hex");
  return `sha256:${digest}`;
}
