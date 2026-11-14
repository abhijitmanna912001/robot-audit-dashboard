"use client";

import { useRef } from "react";
import { usePathname, useRouter } from "next/navigation";
import { MiniPipeline } from "@/components/MiniPipeline";
import { statusDot } from "@/components/status-dot";
import { stepIndex } from "@/lib/list-nav";
import type { PipelineStage } from "@/lib/pipeline";
import { cn } from "@/lib/utils";
import type { CommandStatus } from "@/types/draft";
import { ALL } from "./constants";

export type ScenarioRow = { id: string; title: string; status: CommandStatus; stages: PipelineStage[] };

// Left pane of /dev-preview: a vertical list of scenarios, the stacked "All" view last. The
// selection lives in the URL (?scenario=<id>); the page is a server component that reads it.
// Up/Down/Home/End move the selection while the list has focus.
export function ScenarioList({ rows, current }: { rows: ScenarioRow[]; current: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const ids = [...rows.map((r) => r.id), ALL];
  const index = Math.max(0, ids.indexOf(current));

  function select(id: string) {
    router.replace(`${pathname}?scenario=${encodeURIComponent(id)}`, { scroll: false });
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const next = stepIndex(ids.length, index, e.key);
    if (next === undefined) return;
    e.preventDefault();
    select(ids[next]);
    refs.current[next]?.focus();
  }

  const item = (id: string, n: number, children: React.ReactNode) => (
    <button
      key={id}
      ref={(el) => {
        refs.current[n] = el;
      }}
      id={`scenario-tab-${id}`}
      type="button"
      role="tab"
      aria-selected={n === index}
      aria-controls="scenario-panel"
      tabIndex={n === index ? 0 : -1}
      onClick={() => select(id)}
      className={cn(
        "flex w-full min-w-0 items-center gap-3 rounded-ui-md border px-3 py-2 text-left text-sm leading-5 outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/60",
        n === index
          ? "border-primary/30 border-l-2 border-l-primary bg-primary/12 text-foreground hover:bg-primary/16"
          : "border-transparent text-muted-foreground hover:bg-raised hover:text-foreground active:bg-muted",
      )}
    >
      {children}
    </button>
  );

  return (
    <div
      role="tablist"
      aria-label="Scenarios"
      aria-orientation="vertical"
      onKeyDown={onKeyDown}
      className="flex flex-col gap-1"
    >
      {rows.map((r, n) =>
        item(
          r.id,
          n,
          <>
            <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", statusDot[r.status])} />
            <span className="min-w-0 flex-1 truncate" title={r.title}>
              {r.title}
            </span>
            <MiniPipeline stages={r.stages} />
          </>,
        ),
      )}
      {item(
        ALL,
        rows.length,
        <>
          <span aria-hidden="true" className="size-2 shrink-0 rounded-sm border border-muted-foreground/60" />
          <span className="min-w-0 flex-1 truncate">All scenarios (stacked)</span>
        </>,
      )}
    </div>
  );
}
