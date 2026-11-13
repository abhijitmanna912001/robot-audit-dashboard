// POST /api/commands: creates a `commands` row, then its first audit event.
// No auth yet (known limitation, docs/schema-draft.md section 9). The two
// inserts are NOT atomic; see the gap handling below.
import { createServerClient } from "../../../lib/supabase/server.ts";

const MAX_TEXT_LENGTH = 500;

function fail(status: number, error: string, extra: Record<string, unknown> = {}) {
  return Response.json({ error, ...extra }, { status });
}

// Log only a code and message, never the raw error object, so nothing
// unexpected (headers, keys) can end up in the logs.
function describe(e: unknown): string {
  if (typeof e === "object" && e !== null) {
    const { code, message } = e as { code?: unknown; message?: unknown };
    return `${String(code ?? "no-code")}: ${String(message ?? "no-message")}`;
  }
  return String(e);
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return fail(400, "Invalid JSON");
  }

  const raw =
    typeof body === "object" && body !== null && !Array.isArray(body)
      ? (body as Record<string, unknown>).text
      : undefined;
  if (typeof raw !== "string") return fail(400, "text must be a string");

  const text = raw.trim();
  if (text.length === 0) return fail(400, "text must not be empty");
  if (text.length > MAX_TEXT_LENGTH) {
    return fail(400, `text must be at most ${MAX_TEXT_LENGTH} characters`);
  }

  try {
    const supabase = createServerClient();

    // Let the database fill id, status ('received') and timestamps.
    const { data, error } = await supabase
      .from("commands")
      .insert({ text })
      .select("id")
      .single();
    if (error || !data) {
      console.error("commands insert failed:", describe(error ?? "no row returned"));
      return fail(500, "Internal error");
    }
    const commandId: string = data.id;

    const { error: auditError } = await supabase.from("audit_events").insert({
      command_id: commandId,
      event_type: "command_received",
      message: "Command received from dashboard",
      payload: { source: "dashboard" },
    });
    if (auditError) {
      // Gap: the command exists (status 'received') without its first audit
      // event. No retry (could duplicate an append-only row) and no delete
      // (the worker may already have claimed it). Log and tell the caller.
      console.error(
        `audit_events insert failed for command ${commandId}:`,
        describe(auditError),
      );
      return fail(500, "Command saved but audit event failed", { commandId });
    }

    return Response.json({ commandId }, { status: 201 });
  } catch (e) {
    console.error("POST /api/commands failed:", describe(e));
    return fail(500, "Internal error");
  }
}
