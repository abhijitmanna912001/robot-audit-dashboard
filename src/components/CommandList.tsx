"use client";

import { useRef } from "react";
import { Inbox } from "lucide-react";
import type { Command } from "@/types/draft";
import { cn } from "@/lib/utils";
import { LocalTime } from "./LocalTime";
import { MiniPipeline } from "./MiniPipeline";
import { statusDot } from "./status-dot";
import type { PipelineStage } from "@/lib/pipeline";

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
      <div className="flex flex-col items-start gap-3 rounded-ui-md border border-dashed border-input px-4 py-7">
        <Inbox aria-hidden="true" className="size-6 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      </div>
    );
  }
  return (
    <ul ref={list} onKeyDown={onKeyDown} className="scroll-subtle -m-1.5 flex max-h-64 flex-col divide-y divide-border overflow-y-auto p-1.5 lg:max-h-none lg:overflow-visible">
      {commands.map((c) => {
        const selected = c.id === selectedId;
        return (
          <li key={c.id} className="min-w-0 py-1.5">
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={selected ? "true" : undefined}
              className={cn(
                "relative block w-full min-w-0 rounded-ui-sm border-l-3 border-l-transparent px-3 py-3 text-left text-sm text-card-foreground outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/30",
                selected
                  ? "border-l-primary bg-accent hover:bg-accent active:bg-primary/15"
                  : "bg-transparent hover:bg-surface active:bg-muted",
              )}
            >
              <span className="flex min-w-0 items-center justify-between gap-3">
                <span className="block h-10 min-w-0 line-clamp-2 wrap-anywhere font-medium leading-5" title={c.text}>
                  {c.text}
                </span>
                {stagesById?.get(c.id) && <MiniPipeline stages={stagesById.get(c.id)!} />}
              </span>
              <span className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-2 gap-y-1 text-xs leading-4 text-muted-foreground">
                <span className="inline-flex items-center gap-2">
                  <span aria-hidden="true" className={cn("size-1.5 rounded-full", statusDot[c.status])} />
                  <span className="capitalize">{c.status}</span>
                </span>
                <span className="font-mono" title={c.id}>Run {shortId(c.id)}</span>
                <LocalTime iso={c.createdAt} showDate showRelative className="col-span-2 font-mono" />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
