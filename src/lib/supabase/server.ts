import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Server-only client for route handlers. Uses the service-role key, which
// BYPASSES RLS. Never import this from client components, and never expose
// the key through a NEXT_PUBLIC_ variable.
export function createServerClient(): SupabaseClient {
  if (typeof window !== "undefined") {
    throw new Error("createServerClient must not be used in the browser");
  }

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !serviceRoleKey) {
    throw new Error(
      "Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
    );
  }

  return createClient(url, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
