"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  PW_CHANGED,
  PW_CURRENT_WRONG,
  PW_FAILED,
  validatePasswordChange,
} from "@/lib/change-password";
import { SIGN_IN_FAILED, SIGN_IN_RATE_LIMITED, signInMessage } from "@/lib/sign-in-message";
import { getBrowserClient } from "@/lib/supabase/browser";

const input =
  "h-10 rounded-ui-sm border border-input bg-surface px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/30";

// Verifies the current password by signing in with it, then sets the new one. Messages are generic;
// raw errors are never shown. The email is read from the session and never rendered.
export function ChangePasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(e: { preventDefault: () => void }) {
    e.preventDefault();
    if (busy) return;
    const invalid = validatePasswordChange({ current, next, confirm });
    if (invalid) {
      setMessage(invalid);
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const auth = getBrowserClient().auth;
      const { data } = await auth.getUser();
      const email = data.user?.email;
      if (!email) {
        setMessage(PW_FAILED);
        return;
      }
      const { error: verifyError } = await auth.signInWithPassword({ email, password: current });
      if (verifyError) {
        const m = signInMessage(verifyError);
        // Only a credential failure means the current password is wrong; outages are not that.
        setMessage(m === SIGN_IN_FAILED ? PW_CURRENT_WRONG : m === SIGN_IN_RATE_LIMITED ? m : PW_FAILED);
        return;
      }
      const { error } = await auth.updateUser({ password: next });
      if (error) {
        setMessage(PW_FAILED);
        return;
      }
      setCurrent("");
      setNext("");
      setConfirm("");
      setMessage(PW_CHANGED);
    } catch {
      setMessage(PW_FAILED);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <label htmlFor="current-password" className="text-sm font-medium">
        Current password
      </label>
      <input
        id="current-password"
        type="password"
        autoComplete="current-password"
        required
        value={current}
        onChange={(e) => setCurrent(e.target.value)}
        className={input}
      />
      <label htmlFor="new-password" className="text-sm font-medium">
        New password
      </label>
      <input
        id="new-password"
        type="password"
        autoComplete="new-password"
        required
        minLength={12}
        value={next}
        onChange={(e) => setNext(e.target.value)}
        className={input}
      />
      <label htmlFor="confirm-password" className="text-sm font-medium">
        Confirm new password
      </label>
      <input
        id="confirm-password"
        type="password"
        autoComplete="new-password"
        required
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
        className={input}
      />
      <Button type="submit" disabled={busy} className="h-10 rounded-ui-sm">
        {busy ? "Changing..." : "Change password"}
      </Button>
      {message && (
        <p role="status" className="text-sm text-muted-foreground">
          {message}
        </p>
      )}
    </form>
  );
}
