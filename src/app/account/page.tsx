import { Suspense } from "react";
import Link from "next/link";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { Brand } from "@/components/Header";
import { createAuthServerClient } from "@/lib/supabase/auth-server";
import { ChangePasswordForm } from "./ChangePasswordForm";

export const metadata = { title: "Account" };

// Signed-in only. Reads cookies (runtime data), so it sits inside <Suspense>. Any failure to
// identify the user sends the visitor to /sign-in. The email is never shown.
async function Guarded() {
  let signedIn = false;
  try {
    const auth = createAuthServerClient(await cookies());
    const { data } = await auth.auth.getUser();
    signedIn = !!data.user;
  } catch {
    /* env missing or Auth unreachable: treated as signed out */
  }
  if (!signedIn) redirect("/sign-in");
  return <ChangePasswordForm />;
}

export default function AccountPage() {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-md flex-col gap-8 px-4 py-10">
      <Brand />
      <section aria-label="Change password" className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold">Change password</h2>
        <p className="text-sm text-muted-foreground">Use at least 12 characters, different from your current password.</p>
        <Suspense fallback={null}>
          <Guarded />
        </Suspense>
      </section>
      <Link href="/" className="text-sm text-primary underline underline-offset-4">
        Back to the dashboard
      </Link>
    </main>
  );
}
