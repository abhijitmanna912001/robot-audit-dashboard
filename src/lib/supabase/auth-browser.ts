import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

// Browser Auth client (sign-in link only): anon key, cookie storage so the PKCE verifier survives
// the trip through the email link. Realtime keeps using browser.ts, unchanged.
export function createAuthBrowserClient(): SupabaseClient {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anonKey) {
    throw new Error("Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY");
  }
  return createBrowserClient(url, anonKey);
}
