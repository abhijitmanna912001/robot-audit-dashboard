"use client";

import { useRef } from "react";
import { Inbox } from "lucide-react";
import type { Command, CommandStatus } from "@/types/draft";
import { cn } from "@/lib/utils";
import { LocalTime } from "./LocalTime";
import { MiniPipeline } from "./MiniPipeline";
import type { PipelineStage } from "@/lib/pipeline";

// Literal class names so Tailwind can see them. A small dot plus the status word.
const dot: Record<CommandStatus, string> = {
  received: "bg-status-received-accent",
  approved: "bg-status-approved-accent",
  rejected: "bg-status-rejected-accent",
  executing: "bg-status-executing-accent",
  completed: "bg-status-completed-accent",
  failed: "bg-status-failed-accent",
};

// First 8 characters of the id, as plain text (ids are uuids; the row also has a title).
const shortId = (id: string) => id.slice(0, 8);

export function CommandList({
  commands,
  selectedId,
  onSelect,
  stagesById,
  emptyText = "No commands yet. Type one above and it will appear here.",
}: {
  commands: Command[];
  selectedId?: string;
  onSelect: (id: string) => void;
  stagesById?: ReadonlyMap<string, readonly PipelineStage[]>;
  emptyText?: string;
}) {
  const list = useRef<HTMLUListElement>(null);

  // Up/Down move focus between rows. Tab still works; this is only a shortcut.
  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = Array.from(list.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    const i = rows.indexOf(document.activeElement as HTMLButtonElement);
    if (i === -1) return;
    const next = rows[i + (e.key === "ArrowDown" ? 1 : -1)];
    if (next) {
      e.preventDefault();
      next.focus({ preventScroll: true });
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      next.scrollIntoView({ block: "nearest", behavior: calm ? "auto" : "smooth" });
    }
  }

  if (commands.length === 0) {
    return (
      <div className="surface-card flex flex-col items-center gap-2 rounded-ui-lg px-4 py-8 text-center">
        <Inbox aria-hidden="true" className="size-6 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      </div>
    );
  }
  return (
    <ul ref={list} onKeyDown={onKeyDown} className="flex flex-col gap-2">
      {commands.map((c) => {
        const selected = c.id === selectedId;
        return (
          <li key={c.id} className="min-w-0">
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={selected ? "true" : undefined}
              className={cn(
                "block min-h-16 w-full min-w-0 rounded-ui-md border px-3 py-2 text-left text-sm text-card-foreground outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/60",
                selected
                  ? "border-primary/30 border-l-2 border-l-primary bg-primary/12 hover:bg-primary/16 active:bg-primary/20"
                  : "surface-card hover:bg-raised active:bg-muted",
              )}
            >
              <span className="flex min-w-0 items-center justify-between gap-3">
                <span className="block min-w-0 truncate font-medium" title={c.text}>
                  {c.text}
                </span>
                {stagesById?.get(c.id) && <MiniPipeline stages={stagesById.get(c.id)!} />}
              </span>
              <span className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1.5">
                  <span aria-hidden="true" className={cn("size-1.5 rounded-full", dot[c.status])} />
                  <span className="capitalize">{c.status}</span>
                </span>
                <span className="font-mono">{shortId(c.id)}</span>
                <LocalTime iso={c.createdAt} showDate className="font-mono" />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
