import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Browser-safe client: anon key only. Access is limited by RLS policies.
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

  client = createClient(url, anonKey);
  return client;
}
