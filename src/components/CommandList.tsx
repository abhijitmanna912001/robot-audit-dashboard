"use client";

import { useRef } from "react";
import { Inbox } from "lucide-react";
import type { Command, CommandStatus } from "@/types/draft";
import { cn } from "@/lib/utils";
import { LocalTime } from "./LocalTime";
import { StatusBadge } from "./StatusBadge";

// Literal class names so Tailwind can see them. The left border carries the status colour.
const leftBorder: Record<CommandStatus, string> = {
  received: "border-l-status-received-accent",
  approved: "border-l-status-approved-accent",
  rejected: "border-l-status-rejected-accent",
  executing: "border-l-status-executing-accent",
  completed: "border-l-status-completed-accent",
  failed: "border-l-status-failed-accent",
};

// First 8 characters of the id, as plain text (ids are uuids; the row also has a title).
const shortId = (id: string) => id.slice(0, 8);

export function CommandList({
  commands,
  selectedId,
  onSelect,
  emptyText = "No commands yet. Type one above and it will appear here.",
}: {
  commands: Command[];
  selectedId?: string;
  onSelect: (id: string) => void;
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
      <div className="flex flex-col items-center gap-2 rounded-ui-lg bg-surface px-4 py-8 text-center">
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
                "block min-h-16 w-full min-w-0 rounded-ui-md border-l-[3px] px-3 py-2 text-left text-sm text-card-foreground outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/60",
                leftBorder[c.status],
                selected
                  ? "bg-primary/20"
                  : "bg-surface hover:bg-raised",
              )}
            >
              <span className="block truncate font-medium" title={c.text}>
                {c.text}
              </span>
              <span className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                <StatusBadge status={c.status} />
                <span className="font-mono">{shortId(c.id)}</span>
                <LocalTime iso={c.createdAt} showDate />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
