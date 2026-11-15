"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { createAuthBrowserClient } from "@/lib/supabase/auth-browser";

const SENT = "If this email can sign in, a link is on its way. Check your inbox.";
const FAILED = "Could not send the link. Try again in a minute.";

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
      const auth = createAuthBrowserClient();
      const { error } = await auth.auth.signInWithOtp({
        email: value,
        options: { shouldCreateUser: false, emailRedirectTo: `${window.location.origin}/auth/callback` },
      });
      // Unknown users also error (sign-ups are off): show the same text. Only a rate limit or a
      // network failure, which say nothing about the address, get the failure text.
      const status = (error as { status?: number } | null)?.status;
      setMessage(error && (status === 429 || status === undefined || status >= 500) ? FAILED : SENT);
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
