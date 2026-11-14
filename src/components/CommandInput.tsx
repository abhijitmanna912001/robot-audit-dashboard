"use client";

import { useId, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";

const MAX_TEXT_LENGTH = 500; // mirrors the route; the server stays the authority
const GENERIC = "Could not submit the command. Nothing was saved. Try again.";
const NETWORK = "Could not reach the server. Try again.";
const SAVED_AUDIT_FAILED = "Command saved, but its audit event failed. Do not resubmit.";

const EXAMPLES = [
  "set joint _1 to 0.25 rad",
  "set joint _6 to -1.0 rad",
  "Pick up the cup and move it to the left",
];

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
  const id = useId();
  const helpId = `${id}-help`;
  const countId = `${id}-count`;

  async function handleSubmit(e: { preventDefault: () => void }) {
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
        toast.error(NETWORK);
        return;
      }

      const body = await readJson(res);
      const commandId =
        typeof body.commandId === "string" && body.commandId ? body.commandId : undefined;

      if (res.status === 201) {
        setText("");
        toast.success("Command submitted");
        onSubmitted(commandId);
      } else if (res.status === 400) {
        const msg = typeof body.error === "string" ? body.error.slice(0, 200) : GENERIC;
        setNotice({ kind: "error", message: msg });
        toast.error(msg);
      } else if (res.status === 500 && commandId) {
        // Saved, but its audit event failed. Clear the input and offer no retry.
        setText("");
        setNotice({ kind: "warning", message: SAVED_AUDIT_FAILED });
        toast.warning(SAVED_AUDIT_FAILED, { duration: 10000 });
        onSubmitted(commandId);
      } else {
        setNotice({ kind: "error", message: GENERIC });
        toast.error(GENERIC);
      }
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <Card size="sm">
        <CardContent className="flex flex-col gap-3">
          <label htmlFor={id} className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            New command
          </label>
          <Textarea
            id={id}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setNotice(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
                void handleSubmit(e);
              }
            }}
            rows={3}
            maxLength={MAX_TEXT_LENGTH}
            placeholder="Type a command for the robot..."
            aria-describedby={`${helpId} ${countId}`}
            className="min-h-20 w-full resize-y"
          />
          <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <p id={helpId} className="min-w-0 flex-1 basis-64">
              Format: set joint _1 to 0.25 rad; joints _1 to _6; unit rad; separate several with ;
            </p>
            <span id={countId} className="tabular-nums">
              {text.length}/{MAX_TEXT_LENGTH}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2" aria-label="Examples">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => {
                  setText(ex);
                  setNotice(null);
                }}
                className="wrap-anywhere rounded-full border border-border bg-muted/40 px-3 py-1 text-left text-xs text-foreground/90 outline-none transition-colors duration-150 hover:bg-accent/60 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {ex}
              </button>
            ))}
          </div>
          {notice && (
            <p
              role="alert"
              className={`text-sm ${
                notice.kind === "warning" ? "text-status-failed-accent" : "text-destructive"
              }`}
            >
              {notice.message}
            </p>
          )}
          <div className="flex items-center justify-end gap-3">
            <span className="hidden text-xs text-muted-foreground sm:inline">Cmd/Ctrl+Enter to submit</span>
            <Button type="submit" disabled={submitting} className="px-4">
              {submitting ? "Submitting..." : "Submit"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </form>
  );
}
