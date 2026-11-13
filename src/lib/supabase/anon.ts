import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Anon-key client for server-side READS (server components). Not a singleton, so
// nothing is shared between requests. RLS limits it to SELECT. The service-role
// client in server.ts must not be used for reads.
export function createAnonServerClient(): SupabaseClient {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anonKey) {
    throw new Error("Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY");
  }
  return createClient(url, anonKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
