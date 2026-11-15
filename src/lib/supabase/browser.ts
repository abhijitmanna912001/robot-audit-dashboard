import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

// THE one browser Supabase client (anon key only; access is limited by RLS). The Realtime
// subscription and the sign-in link both use it, so there is a single GoTrueClient. It is the
// @supabase/ssr client so the session and the PKCE verifier live in cookies the server can read.
// Never import the service-role key here.
let client: SupabaseClient | undefined;

export function getBrowserClient(): SupabaseClient {
  if (client) return client;

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anonKey) {
    throw new Error(
      "Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY",
    );
  }

  client = createBrowserClient(url, anonKey);
  return client;
}
