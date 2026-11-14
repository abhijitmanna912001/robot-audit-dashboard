// Human label for a raw planner or critic error kind. Pure, display only, never throws.
// Known kinds get a fixed label; any other string is shown as the capped text; anything that
// is not a non-empty string gives undefined so callers can leave it out.
import { text } from "./payload.ts";

const LABELS: Readonly<Record<string, string>> = {
  unsupported_command: "Not a supported command",
  planner_error: "Planner error",
  http_status: "Critic returned an HTTP error",
  timeout: "Critic timed out",
  invalid_body: "Critic returned an invalid response",
};

const MAX_KIND = 40;

export function isKnownKind(kind: unknown): boolean {
  return typeof kind === "string" && Object.hasOwn(LABELS, kind.trim());
}

export function kindLabel(kind: unknown): string | undefined {
  if (typeof kind !== "string") return undefined;
  const key = kind.trim();
  if (Object.hasOwn(LABELS, key)) return LABELS[key];
  const t = text(kind, MAX_KIND);
  return t === undefined ? undefined : t.replace(/\s+/g, " ").trim();
}
