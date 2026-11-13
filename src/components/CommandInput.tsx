"use client";

import { useRef, useState } from "react";

const MAX_TEXT_LENGTH = 500; // mirrors the route; the server stays the authority
const GENERIC = "Could not submit the command. Nothing was saved. Try again.";
const NETWORK = "Could not reach the server. Try again.";
const SAVED_AUDIT_FAILED = "Command saved, but its audit event failed. Do not resubmit.";

type Notice = { kind: "error" | "warning"; message: string };

async function readJson(res: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await res.json();
    return typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export function CommandInput({ onSubmitted }: { onSubmitted: (commandId?: string) => void }) {
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const inFlight = useRef(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (inFlight.current || !text.trim()) return;
    inFlight.current = true;
    setSubmitting(true);
    setNotice(null);

    try {
      let res: Response;
      try {
        res = await fetch("/api/commands", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ text }),
        });
      } catch {
        setNotice({ kind: "error", message: NETWORK });
        return;
      }

      const body = await readJson(res);
      const commandId =
        typeof body.commandId === "string" && body.commandId ? body.commandId : undefined;

      if (res.status === 201) {
        setText("");
        onSubmitted(commandId);
      } else if (res.status === 400) {
        const msg = typeof body.error === "string" ? body.error.slice(0, 200) : GENERIC;
        setNotice({ kind: "error", message: msg });
      } else if (res.status === 500 && commandId) {
        // Saved, but its audit event failed. Clear the input and offer no retry.
        setText("");
        setNotice({ kind: "warning", message: SAVED_AUDIT_FAILED });
        onSubmitted(commandId);
      } else {
        setNotice({ kind: "error", message: GENERIC });
      }
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2">
      <div className="flex flex-col gap-2 sm:flex-row">
        <textarea
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setNotice(null);
          }}
          rows={2}
          maxLength={MAX_TEXT_LENGTH}
          placeholder="Type a command for the robot..."
          className="flex-1 rounded-md border border-zinc-300 bg-transparent p-2 text-sm dark:border-zinc-700"
        />
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {submitting ? "Submitting..." : "Submit"}
        </button>
      </div>
      {notice && (
        <p
          role="alert"
          className={`text-sm ${
            notice.kind === "warning" ? "text-orange-600" : "text-red-600"
          }`}
        >
          {notice.message}
        </p>
      )}
    </form>
  );
}
