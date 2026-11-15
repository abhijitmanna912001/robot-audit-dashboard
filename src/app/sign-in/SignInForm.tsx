"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { SIGN_IN_FAILED as FAILED, signInMessage } from "@/lib/sign-in-message";
import { getBrowserClient } from "@/lib/supabase/browser";

// Email magic link. The answer is the same whether or not the email is allowlisted, so this form
// never reveals who is an operator. The server route checks the allowlist on every submission.
export function SignInForm({ failedBefore }: { failedBefore: boolean }) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(
    failedBefore ? "That sign-in link did not work. Request a new one." : null,
  );

  async function submit(e: { preventDefault: () => void }) {
    e.preventDefault();
    const value = email.trim();
    if (busy || !value) return;
    setBusy(true);
    try {
      const auth = getBrowserClient();
      const { error } = await auth.auth.signInWithOtp({
        email: value,
        options: { shouldCreateUser: false, emailRedirectTo: `${window.location.origin}/auth/callback` },
      });
      // The message depends only on the error code and status, never on its text or the address.
      setMessage(signInMessage(error));
    } catch {
      setMessage(FAILED);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <label htmlFor="email" className="text-sm font-medium">
        Email
      </label>
      <input
        id="email"
        type="email"
        autoComplete="email"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="h-10 rounded-ui-sm border border-input bg-surface px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/30"
      />
      <Button type="submit" disabled={busy} className="h-10 rounded-ui-sm">
        {busy ? "Sending..." : "Email me a sign-in link"}
      </Button>
      {message && (
        <p role="status" className="text-sm text-muted-foreground">
          {message}
        </p>
      )}
    </form>
  );
}
