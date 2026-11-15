// Sign-out. POST only, so a link or image on another site cannot sign anyone out.
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { createAuthServerClient } from "../../../lib/supabase/auth-server.ts";

export async function POST(request: Request) {
  try {
    const auth = createAuthServerClient(await cookies());
    await auth.auth.signOut();
  } catch {
    /* the redirect below is the same either way */
  }
  return NextResponse.redirect(new URL("/", request.url), 303);
}
