"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, X } from "lucide-react";
import { Button } from "@/components/ui/button";

type State = "idle" | "copied" | "failed";

// Copies a value (a hash) to the clipboard. Display helper only. It never throws: an
// unavailable or failing Clipboard API shows a brief "Copy failed" state instead.
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<State>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    let next: State = "copied";
    try {
      if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
        next = "failed";
      } else {
        await navigator.clipboard.writeText(value);
      }
    } catch {
      next = "failed";
    }
    setState(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 1500);
  }

  const Icon = state === "copied" ? Check : state === "failed" ? X : Copy;
  const status = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "";
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        onClick={copy}
        aria-label={`Copy ${label}`}
        title={status || `Copy ${label}`}
        className="text-muted-foreground transition-colors duration-150 hover:bg-raised hover:text-foreground active:bg-muted"
      >
        <Icon aria-hidden="true" />
      </Button>
      <span role="status" aria-live="polite" className="sr-only">
        {status}
      </span>
    </>
  );
}
