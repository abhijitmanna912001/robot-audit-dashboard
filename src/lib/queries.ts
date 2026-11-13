// Read queries shared by the server page and the browser refetch. The client is
// passed in, so each side uses its own anon-key client.
import type { SupabaseClient } from "@supabase/supabase-js";
import { toAuditEvent, toCommand } from "./mappers.ts";
import type { AuditEvent, Command } from "../types/draft.ts";

export const COMMAND_LIMIT = 50;
const EVENT_LIMIT = 1000;

export type Snapshot = { commands: Command[]; events: AuditEvent[] };

// Log only a code and message, never the raw error object.
function describe(e: unknown): string {
  if (typeof e === "object" && e !== null) {
    const { code, message } = e as { code?: unknown; message?: unknown };
    return `${String(code ?? "no-code")}: ${String(message ?? "no-message")}`;
  }
  return String(e);
}

export async function fetchSnapshot(
  client: SupabaseClient,
): Promise<{ ok: true; data: Snapshot } | { ok: false }> {
  try {
    const { data: cmdRows, error } = await client
      .from("commands")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(COMMAND_LIMIT);
    if (error || !cmdRows) {
      console.error("commands read failed:", describe(error ?? "no data"));
      return { ok: false };
    }
    const commands = cmdRows.map(toCommand).filter((c): c is Command => c !== null);
    if (commands.length === 0) return { ok: true, data: { commands, events: [] } };

    const { data: evRows, error: evError } = await client
      .from("audit_events")
      .select("*")
      .in("command_id", commands.map((c) => c.id))
      .order("created_at", { ascending: true })
      .limit(EVENT_LIMIT);
    if (evError || !evRows) {
      console.error("audit_events read failed:", describe(evError ?? "no data"));
      return { ok: false };
    }
    const events = evRows.map(toAuditEvent).filter((e): e is AuditEvent => e !== null);
    return { ok: true, data: { commands, events } };
  } catch (e) {
    console.error("snapshot read failed:", describe(e));
    return { ok: false };
  }
}
