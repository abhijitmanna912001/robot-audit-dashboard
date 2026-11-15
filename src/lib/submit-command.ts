// Logic of POST /api/commands, with its dependencies injected so tests need no Next runtime and no
// network. The route file wires the real ones.
//
// Order of checks, all BEFORE any insert (each failure is generic and leaks nothing):
//   1. 401  no valid session (user verified server-side through supabase.auth.getUser())
//   2. 500  OPERATORS unset or malformed (fail closed)
//   3. 403  the user's email is not in OPERATORS
//   4. 400  body validation (unchanged)
//   5. 429  rate limit, counted from the commands table by user id
// Then: insert the command, then its command_received event. The email is used only to look up the
// display name; it is never stored, returned or logged. Only the display name and the Auth user id
// are written.
import type { SupabaseClient } from "@supabase/supabase-js";
import { lookupOperator, parseOperators } from "./operators.ts";

export const MAX_TEXT_LENGTH = 500;
export const RATE_LIMIT_PER_MINUTE = 5;
export const RATE_LIMIT_PER_DAY = 50;
const MINUTE_MS = 60_000;
const DAY_MS = 24 * 60 * MINUTE_MS;

export type SessionUser = { id: string; email?: string | null };

export type SubmitDeps = {
  // Verified server-side (getUser), never a client-supplied identity. Null when not signed in.
  getUser: () => Promise<SessionUser | null>;
  // Service-role client (bypasses RLS). Server only.
  db: () => SupabaseClient;
  operatorsRaw: string | undefined;
  now: () => Date;
};

function fail(status: number, error: string, extra: Record<string, unknown> = {}) {
  return Response.json({ error, ...extra }, { status });
}

// Log only a code and message, never the raw error object, so nothing unexpected (headers, keys)
// can end up in the logs.
function describe(e: unknown): string {
  if (typeof e === "object" && e !== null) {
    const { code, message } = e as { code?: unknown; message?: unknown };
    return `${String(code ?? "no-code")}: ${String(message ?? "no-message")}`;
  }
  return String(e);
}

export async function handleSubmit(request: Request, deps: SubmitDeps): Promise<Response> {
  try {
    // 1. Session.
    let user: SessionUser | null = null;
    try {
      user = await deps.getUser();
    } catch (e) {
      console.error("session check failed:", describe(e));
    }
    if (!user || typeof user.id !== "string" || user.id === "") return fail(401, "Unauthorized");

    // 2. Allowlist configuration. The value is never logged.
    const operators = parseOperators(deps.operatorsRaw);
    if (!operators.ok) {
      console.error("OPERATORS is unset or malformed");
      return fail(500, "Internal error");
    }

    // 3. Allowlist.
    const displayName = lookupOperator(operators.byEmail, user.email);
    if (displayName === undefined) return fail(403, "Forbidden");

    // 4. Validation (unchanged).
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

    const supabase = deps.db();

    // 5. Rate limit. A failed count fails closed: nothing is inserted.
    const now = deps.now().getTime();
    const countSince = async (ms: number): Promise<number | undefined> => {
      const { count, error } = await supabase
        .from("commands")
        .select("id", { count: "exact", head: true })
        .eq("submitted_by_id", user.id)
        .gte("created_at", new Date(now - ms).toISOString());
      if (error || typeof count !== "number") {
        console.error("rate-limit count failed:", describe(error ?? "no count returned"));
        return undefined;
      }
      return count;
    };
    const lastMinute = await countSince(MINUTE_MS);
    if (lastMinute === undefined) return fail(500, "Internal error");
    if (lastMinute >= RATE_LIMIT_PER_MINUTE) return fail(429, "Too many requests");
    const lastDay = await countSince(DAY_MS);
    if (lastDay === undefined) return fail(500, "Internal error");
    if (lastDay >= RATE_LIMIT_PER_DAY) return fail(429, "Too many requests");

    // Let the database fill id, status ('received') and timestamps.
    const { data, error } = await supabase
      .from("commands")
      .insert({ text, submitted_by: displayName, submitted_by_id: user.id })
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
      payload: { source: "dashboard", submitted_by: displayName, submitted_by_id: user.id },
    });
    if (auditError) {
      // Gap: the command exists (status 'received') without its first audit event. No retry (could
      // duplicate an append-only row) and no delete (the worker may already have claimed it).
      console.error(`audit_events insert failed for command ${commandId}:`, describe(auditError));
      return fail(500, "Command saved but audit event failed", { commandId });
    }

    return Response.json({ commandId }, { status: 201 });
  } catch (e) {
    console.error("POST /api/commands failed:", describe(e));
    return fail(500, "Internal error");
  }
}
