"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { SIGN_IN_UNAVAILABLE, signInMessage } from "@/lib/sign-in-message";
import { getBrowserClient } from "@/lib/supabase/browser";

const input =
  "h-10 rounded-ui-sm border border-input bg-surface px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/30";

// Email + password. Every failure shows one generic message (see signInMessage), so this form never
// reveals whether an email exists or is an operator. The server route checks the allowlist on every
// submission.
export function SignInForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(e: { preventDefault: () => void }) {
    e.preventDefault();
    const value = email.trim();
    if (busy || !value || !password) return;
    setBusy(true);
    setMessage(null);
    try {
      const { error } = await getBrowserClient().auth.signInWithPassword({ email: value, password });
      if (error) {
        setMessage(signInMessage(error));
        return;
      }
      router.replace("/");
      router.refresh();
    } catch {
      setMessage(SIGN_IN_UNAVAILABLE); // thrown: the request never completed
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
        className={input}
      />
      <label htmlFor="password" className="text-sm font-medium">
        Password
      </label>
      <input
        id="password"
        type="password"
        autoComplete="current-password"
        required
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className={input}
      />
      <Button type="submit" disabled={busy} className="h-10 rounded-ui-sm">
        {busy ? "Signing in..." : "Sign in"}
      </Button>
      {message && (
        <p role="alert" className="text-sm text-muted-foreground">
          {message}
        </p>
      )}
    </form>
  );
}
