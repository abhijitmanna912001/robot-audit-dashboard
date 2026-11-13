// Pass/fail/error marker for validator_result and critic_result events.
// Reads only payload.outcome (docs/schema-draft.md "Event payloads").
import type { AuditEvent } from "../types/draft.ts";

export type Marker = "pass" | "fail" | "error";

const VALIDATOR: Record<string, Marker> = { pass: "pass", fail: "fail" };
const CRITIC: Record<string, Marker> = { approve: "pass", reject: "fail", error: "error" };

export function getMarker(event: Pick<AuditEvent, "eventType" | "payload">): Marker | undefined {
  const table =
    event.eventType === "validator_result"
      ? VALIDATOR
      : event.eventType === "critic_result"
        ? CRITIC
        : undefined;
  const outcome = event.payload?.outcome;
  if (!table || typeof outcome !== "string") return undefined;
  return Object.hasOwn(table, outcome) ? table[outcome] : undefined;
}
