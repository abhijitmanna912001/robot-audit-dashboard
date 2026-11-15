// Magic-link landing: exchanges the one-time code for a session cookie, then redirects home.
// Every failure redirects to /sign-in?error=1 with no detail.
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { createAuthServerClient } from "../../../lib/supabase/auth-server.ts";
import { safeNextPath } from "../../../lib/safe-next.ts";

export async function GET(request: Request) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get("code");
  const next = safeNextPath(searchParams.get("next"));
  if (code) {
    try {
      const auth = createAuthServerClient(await cookies());
      const { error } = await auth.auth.exchangeCodeForSession(code);
      if (!error) return NextResponse.redirect(`${origin}${next}`);
    } catch {
      /* fall through to the generic failure */
    }
  }
  return NextResponse.redirect(`${origin}/sign-in?error=1`);
}
