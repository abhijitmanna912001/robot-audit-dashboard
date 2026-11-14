"use client";

import { useRef } from "react";
import { usePathname, useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { ALL } from "./fixtures";

type Item = { id: string; title: string };

// The only client piece of /dev-preview. The selection lives in the URL (?scenario=<id>), the
// page itself is a server component that reads it. Left/Right (and Home/End) move between tabs.
export function ScenarioPicker({ scenarios, current }: { scenarios: Item[]; current: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const items: Item[] = [{ id: ALL, title: "All" }, ...scenarios];
  const index = Math.max(0, items.findIndex((i) => i.id === current));

  function select(id: string) {
    router.replace(`${pathname}?scenario=${encodeURIComponent(id)}`, { scroll: false });
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const last = items.length - 1;
    let next: number;
    if (e.key === "ArrowRight") next = index === last ? 0 : index + 1;
    else if (e.key === "ArrowLeft") next = index === 0 ? last : index - 1;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = last;
    else return;
    e.preventDefault();
    select(items[next].id);
    tabs.current[next]?.focus();
  }

  return (
    <div className="space-y-2">
      <div className="sm:hidden">
        <label htmlFor="scenario-select" className="sr-only">
          Scenario
        </label>
        <select
          id="scenario-select"
          value={items[index].id}
          onChange={(e) => select(e.target.value)}
          className="h-9 w-full rounded-ui-md border border-border bg-surface px-2 text-sm transition-colors duration-150 hover:bg-raised focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          {items.map((i) => (
            <option key={i.id} value={i.id}>
              {i.title}
            </option>
          ))}
        </select>
      </div>
      <div
        role="tablist"
        aria-label="Scenario"
        onKeyDown={onKeyDown}
        className="scroll-subtle hidden gap-1 overflow-x-auto pb-1 sm:flex"
      >
        {items.map((i, n) => (
          <button
            key={i.id}
            ref={(el) => {
              tabs.current[n] = el;
            }}
            id={`scenario-tab-${i.id}`}
            type="button"
            role="tab"
            aria-selected={n === index}
            aria-controls="scenario-panel"
            tabIndex={n === index ? 0 : -1}
            onClick={() => select(i.id)}
            className={cn(
              "h-8 shrink-0 rounded-full border border-border px-3 text-xs whitespace-nowrap text-muted-foreground outline-none transition-colors duration-150 hover:bg-raised hover:text-foreground active:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50",
              n === index && "border-primary/60 bg-primary/15 text-foreground hover:bg-primary/25",
            )}
          >
            {i.title}
          </button>
        ))}
      </div>
    </div>
  );
}
