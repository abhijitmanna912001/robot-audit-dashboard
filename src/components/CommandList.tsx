"use client";

import { useRef } from "react";
import { Inbox } from "lucide-react";
import type { Command } from "@/types/draft";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";
import { LocalTime } from "./LocalTime";
import { MiniPipeline } from "./MiniPipeline";
import { statusDot } from "./status-dot";
import type { PipelineStage } from "@/lib/pipeline";

export function CommandList({
  commands,
  selectedId,
  onSelect,
  stagesById,
  collapsedIds,
  emptyText = "No commands yet. Type one in the command bar and it will appear here.",
}: {
  commands: Command[];
  selectedId?: string;
  onSelect: (id: string) => void;
  stagesById?: ReadonlyMap<string, readonly PipelineStage[]>;
  // Rows hidden below lg until "Show all". Display only; they stay visible on desktop.
  collapsedIds?: ReadonlySet<string>;
  emptyText?: string;
}) {
  const list = useRef<HTMLUListElement>(null);

  // Up/Down move focus between rows. Tab still works; this is only a shortcut. Rows hidden by
  // the narrow-screen collapse are skipped (display:none cannot take focus).
  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = Array.from(list.current?.querySelectorAll<HTMLButtonElement>("button") ?? []).filter(
      (b) => b.offsetParent !== null,
    );
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
      <div className="flex flex-col items-start gap-3 rounded-ui-md border border-dashed border-input px-4 py-6">
        <Inbox aria-hidden="true" className="size-5 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      </div>
    );
  }
  return (
    <ul ref={list} onKeyDown={onKeyDown} className="flex flex-col gap-1">
      {commands.map((c) => {
        const selected = c.id === selectedId;
        const stages = stagesById?.get(c.id);
        return (
          <li key={c.id} className={cn("min-w-0", collapsedIds?.has(c.id) && "max-lg:hidden")}>
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={selected ? "true" : undefined}
              title={`Run ${c.id} · ${formatDateTime(c.createdAt)}`}
              className={cn(
                "flex min-h-11 w-full min-w-0 flex-col gap-0.5 rounded-ui-sm border px-3 py-2 text-left text-sm leading-5 outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/30",
                selected
                  ? "border-primary border-l-2 border-l-primary bg-accent text-primary hover:bg-primary/15"
                  : "border-transparent text-card-foreground hover:bg-raised active:bg-muted",
              )}
            >
              <span className="flex min-w-0 items-center gap-3">
                <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", statusDot[c.status])} />
                <span className="min-w-0 flex-1 truncate font-medium">{c.text}</span>
                {stages && <MiniPipeline stages={stages} />}
              </span>
              <span className="flex min-w-0 items-center gap-1.5 pl-5 text-xs leading-4 text-muted-foreground">
                <span className="capitalize">{c.status}</span>
                <span aria-hidden="true">·</span>
                <LocalTime iso={c.createdAt} showDate className="font-mono" />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
