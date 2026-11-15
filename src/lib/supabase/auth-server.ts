import { createServerClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

// Cookie-backed Auth client for server code (route handlers, server components). Anon key only:
// it identifies the signed-in user, it does not read or write data. The service-role client in
// server.ts is a different thing and stays separate.
type CookieStore = {
  getAll: () => { name: string; value: string }[];
  set: (name: string, value: string, options?: Record<string, unknown>) => void;
};

export function createAuthServerClient(store: CookieStore): SupabaseClient {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anonKey) {
    throw new Error("Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY");
  }
  return createServerClient(url, anonKey, {
    cookies: {
      getAll: () => store.getAll(),
      setAll: (list) => {
        // Server components cannot set cookies; the proxy refreshes the session instead.
        try {
          for (const { name, value, options } of list) store.set(name, value, options);
        } catch {
          /* read-only context */
        }
      },
    },
  });
}
