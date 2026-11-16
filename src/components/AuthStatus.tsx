import Link from "next/link";
import { LogIn, LogOut, UserRound } from "lucide-react";
import type { Access } from "@/lib/access";

// Header slot: who is signed in (display name only, never the email) and the sign-in/out control.
// Server-safe: sign-out is a plain form POST.
export function AuthStatus({ access }: { access: Access }) {
  const link =
    "inline-flex min-h-8 items-center gap-1.5 rounded-ui-sm px-2 text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/30";
  if (access.state === "signed_out") {
    return (
      <Link href="/sign-in" className={link}>
        <LogIn aria-hidden="true" className="size-3.5" />
        Sign in
      </Link>
    );
  }
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-foreground">
        {access.state === "operator" ? access.name : "Signed in, not authorised"}
      </span>
      <Link href="/account" className={link}>
        <UserRound aria-hidden="true" className="size-3.5" />
        Account
      </Link>
      <form action="/auth/signout" method="post">
        <button type="submit" className={link}>
          <LogOut aria-hidden="true" className="size-3.5" />
          Sign out
        </button>
      </form>
    </div>
  );
}
