// The ONE place database rows (snake_case) become app types (camelCase).
// Rows that do not look right map to null so callers can skip them.
import {
  COMMAND_STATUSES,
  EVENT_TYPES,
  type AuditEvent,
  type Command,
  type CommandStatus,
  type EventType,
  type JsonObject,
} from "../types/draft.ts";

export type CommandRow = {
  id: string;
  text: string;
  status: string;
  plan_id: string | null;
  claimed_by: string | null;
  claimed_at: string | null;
  submitted_by?: string | null; // pending db/004; absent on older rows
  created_at: string;
  updated_at: string;
};

export type AuditEventRow = {
  id: string;
  command_id: string;
  event_type: string;
  message: string;
  payload: unknown;
  created_at: string;
};

const isStr = (v: unknown): v is string => typeof v === "string";
const strOrNull = (v: unknown): string | null => (isStr(v) ? v : null);

// A display name from the public column: a non-blank string, trimmed and capped. Anything else is null.
const MAX_NAME = 40;
function displayName(v: unknown): string | null {
  if (!isStr(v)) return null;
  const t = v.trim().slice(0, MAX_NAME);
  return t === "" ? null : t;
}

export function toCommand(row: unknown): Command | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Partial<Record<keyof CommandRow, unknown>>;
  if (
    !isStr(r.id) ||
    !isStr(r.text) ||
    !isStr(r.status) ||
    !(COMMAND_STATUSES as readonly string[]).includes(r.status) ||
    !isStr(r.created_at) ||
    !isStr(r.updated_at)
  ) {
    return null;
  }
  return {
    id: r.id,
    text: r.text,
    status: r.status as CommandStatus,
    planId: strOrNull(r.plan_id),
    claimedBy: strOrNull(r.claimed_by),
    claimedAt: strOrNull(r.claimed_at),
    submittedBy: displayName(r.submitted_by),
    createdAt: r.created_at,
    updatedAt: r.updated_at,
  };
}

export function toAuditEvent(row: unknown): AuditEvent | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Partial<Record<keyof AuditEventRow, unknown>>;
  if (
    !isStr(r.id) ||
    !isStr(r.command_id) ||
    !isStr(r.event_type) ||
    !(EVENT_TYPES as readonly string[]).includes(r.event_type) ||
    !isStr(r.message) ||
    !isStr(r.created_at)
  ) {
    return null;
  }
  const payload: JsonObject =
    typeof r.payload === "object" && r.payload !== null && !Array.isArray(r.payload)
      ? (r.payload as JsonObject)
      : {};
  return {
    id: r.id,
    commandId: r.command_id,
    eventType: r.event_type as EventType,
    message: r.message,
    payload,
    createdAt: r.created_at,
  };
}
