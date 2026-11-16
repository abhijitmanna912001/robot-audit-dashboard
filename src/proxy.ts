// Refreshes the Supabase Auth cookie before server code runs, so an expired access token is
// renewed (server components cannot set cookies). Only the paths that read the session; everything
// else, including /dev-preview and /api/critic, is untouched. No redirects, no blocking: reading the
// dashboard stays public and /api/commands does its own checks.
import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anonKey) return response;

  const supabase = createServerClient(url, anonKey, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: (list) => {
        for (const { name, value } of list) request.cookies.set(name, value);
        response = NextResponse.next({ request });
        for (const { name, value, options } of list) response.cookies.set(name, value, options);
      },
    },
  });
  try {
    await supabase.auth.getUser(); // validates and refreshes; the result is not used here
  } catch {
    /* never block a request because Auth is unreachable */
  }
  return response;
}

export const config = { matcher: ["/", "/account", "/api/commands"] };
