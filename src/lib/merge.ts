// Merge helpers: by id, so repeated or out-of-order delivery never duplicates.
import type { AuditEvent, Command } from "../types/draft.ts";

function time(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

// Newest first. An incoming row replaces an existing one only if it is not older
// (by updatedAt), so a slow refetch cannot overwrite a newer live status.
export function mergeCommands(existing: Command[], incoming: Command[]): Command[] {
  const byId = new Map<string, Command>();
  for (const c of existing) byId.set(c.id, c);
  for (const c of incoming) {
    const old = byId.get(c.id);
    if (!old || time(c.updatedAt) >= time(old.updatedAt)) byId.set(c.id, c);
  }
  return [...byId.values()].sort(
    (a, b) => time(b.createdAt) - time(a.createdAt) || cmp(b.id, a.id),
  );
}

// Oldest first. Events are append-only, so a union by id is enough.
export function mergeEvents(existing: AuditEvent[], incoming: AuditEvent[]): AuditEvent[] {
  const byId = new Map<string, AuditEvent>();
  for (const e of existing) byId.set(e.id, e);
  for (const e of incoming) if (!byId.has(e.id)) byId.set(e.id, e);
  return [...byId.values()].sort(
    (a, b) => time(a.createdAt) - time(b.createdAt) || cmp(a.id, b.id),
  );
}
