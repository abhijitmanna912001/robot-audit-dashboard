"use client";

import { useId, useRef, useState } from "react";
import { Info } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

const MAX_TEXT_LENGTH = 500; // mirrors the route; the server stays the authority
const GENERIC = "Could not submit the command. Nothing was saved. Try again.";
const NETWORK = "Could not reach the server. Try again.";
const SAVED_AUDIT_FAILED = "Command saved, but its audit event failed. Do not resubmit.";

const FORMAT_HELP =
  "Format: set joint _1 to 0.25 rad; joints _1 to _6; unit rad; separate several with ;";

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
  const [focused, setFocused] = useState(false); // display only: grows the textarea
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

  const expanded = focused || text.length > 0;
  const showCount = focused || text.length > 400;
  return (
    <form
      onSubmit={handleSubmit}
      // Focus is "inside the composer" while it stays on the textarea or a chip.
      onFocus={() => setFocused(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setFocused(false);
      }}
      className="surface-card flex shrink-0 flex-col gap-3 rounded-ui-lg p-4"
    >
      <label htmlFor={id} className="text-sm font-semibold tracking-tight">
        New command
      </label>
      <div className="flex flex-col gap-3">
        <div className="relative min-w-0">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute top-3 left-3 font-mono text-sm text-primary select-none"
        >
          &gt;
        </span>
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
          rows={expanded ? 2 : 1}
          maxLength={MAX_TEXT_LENGTH}
          placeholder="set joint _1 to 0.25 rad"
          aria-describedby={`${helpId} ${countId}`}
          className="min-h-24 w-full min-w-0 resize-none rounded-ui-sm bg-background pt-3 pb-9 pr-3 pl-7 font-mono text-sm leading-6 caret-primary transition-colors duration-150 md:text-sm"
        />
        <kbd
          aria-hidden="true"
          title="Cmd or Ctrl + Enter submits"
          className="keycap pointer-events-none absolute right-2 bottom-2 rounded-ui-sm px-2 py-1 font-mono text-xs leading-none text-muted-foreground"
        >
          ⌘ ↵
        </kbd>
        </div>
        <Button type="submit" disabled={submitting} className="h-10 w-full rounded-ui-sm px-4 transition-colors duration-150">
          {submitting ? "Submitting..." : "Submit command"}
        </Button>
      </div>
      <div className="flex items-start gap-2 text-xs leading-4 text-muted-foreground">
        <span className="shrink-0 pt-2">Try:</span>
        {/* Examples wrap within the composer, including on narrow screens. */}
        <div className="flex min-w-0 flex-1 flex-wrap items-start gap-2">
        {EXAMPLES.map((ex, i) => (
          <button
            key={ex}
            type="button"
            title={ex}
            aria-label={`Use example: ${ex}`}
            // Keep focus in the textarea so the composer does not collapse when a chip is used.
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              setText(ex);
              setNotice(null);
            }}
            className="keycap inline-flex min-h-8 min-w-0 max-w-full items-center rounded-ui-sm px-2 py-1 text-left font-mono text-xs leading-4 wrap-anywhere whitespace-normal text-muted-foreground outline-none transition-colors duration-150 hover:border-primary hover:text-foreground active:bg-accent focus-visible:ring-3 focus-visible:ring-ring/30"
          >
            {["Joint 1", "Joint 6", "Unsupported"][i]}
          </button>
        ))}
        </div>
      </div>
      <div
        className={`flex items-start justify-between gap-3 text-xs text-muted-foreground ${
          showCount ? "" : "sr-only"
        }`}
      >
        <p
          id={helpId}
          title={FORMAT_HELP}
          className={`flex min-w-0 items-center gap-2 leading-4 ${focused ? "" : "sr-only"}`}
        >
          <Info aria-hidden="true" className="size-3 shrink-0" />
          <span className="wrap-anywhere">{FORMAT_HELP}</span>
        </p>
        <span id={countId} className="shrink-0 tabular-nums">
          {text.length}/{MAX_TEXT_LENGTH}
        </span>
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
    </form>
  );
}
