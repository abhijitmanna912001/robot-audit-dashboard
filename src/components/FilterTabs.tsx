"use client";

import { Button } from "@/components/ui/button";
import type { Tab } from "@/lib/command-filter";
import { cn } from "@/lib/utils";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "attention", label: "Needs attention" },
  { id: "completed", label: "Completed" },
];

// Client-side filters only. The three filters are toggle buttons over one list (not a tablist);
// the test-commands toggle is a switch with the hidden count in its label.
export function FilterTabs({
  tab,
  onTab,
  showTest,
  onShowTest,
  hiddenTestCount,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  showTest: boolean;
  onShowTest: () => void;
  hiddenTestCount: number;
}) {
  const label = !showTest && hiddenTestCount > 0 ? `Test commands (${hiddenTestCount})` : "Test commands";
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-2">
      <div role="group" aria-label="Filter commands" className="flex flex-wrap gap-0.5">
        {TABS.map((t) => (
          <Button
            key={t.id}
            type="button"
            variant="ghost"
            size="xs"
            aria-pressed={tab === t.id}
            onClick={() => onTab(t.id)}
            className={cn(
              "h-7 px-1 text-xs sm:px-1.5 text-muted-foreground transition-colors duration-150 hover:bg-raised hover:text-foreground active:bg-muted",
              tab === t.id && "bg-primary/15 text-foreground hover:bg-primary/25 active:bg-primary/30",
            )}
          >
            {t.label}
          </Button>
        ))}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={showTest}
        onClick={onShowTest}
        className="inline-flex h-7 items-center gap-1.5 rounded-full px-0 text-xs sm:px-1 text-muted-foreground outline-none transition-colors duration-150 hover:text-foreground active:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&:hover>span:first-child]:border-primary/60"
      >
        <span
          aria-hidden="true"
          className={cn(
            "relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border border-border transition-colors duration-150",
            showTest ? "bg-primary" : "bg-raised",
          )}
        >
          <span
            className={cn(
              "absolute left-0.5 size-3 rounded-full bg-foreground transition-transform duration-150",
              showTest && "translate-x-3",
            )}
          />
        </span>
        <span className="tabular-nums">{label}</span>
      </button>
    </div>
  );
}
