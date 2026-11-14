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
    <div className="flex flex-col items-start gap-3">
      <div role="group" aria-label="Filter commands" className="flex w-full flex-wrap gap-1 border-b border-border pb-2">
        {TABS.map((t) => (
          <Button
            key={t.id}
            type="button"
            variant="ghost"
            size="xs"
            aria-pressed={tab === t.id}
            onClick={() => onTab(t.id)}
            className={cn(
              "min-h-9 rounded-ui-sm px-2 text-xs text-muted-foreground transition-colors duration-150 hover:bg-raised hover:text-foreground active:bg-muted",
              tab === t.id && "bg-surface text-primary shadow-xs hover:bg-accent active:bg-accent",
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
        className="inline-flex min-h-9 items-center gap-2 rounded-full px-1 text-xs text-muted-foreground outline-none transition-colors duration-150 hover:text-foreground active:text-foreground focus-visible:ring-3 focus-visible:ring-ring/30 [&:hover>span:first-child]:border-primary"
      >
        <span
          aria-hidden="true"
          className={cn(
            "relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border border-input transition-colors duration-150",
            showTest ? "bg-primary" : "bg-raised",
          )}
        >
          <span
            className={cn(
              "absolute left-0.5 size-3 rounded-full border border-input bg-surface shadow-xs",
              showTest && "translate-x-3",
            )}
          />
        </span>
        <span className="tabular-nums">{label}</span>
      </button>
    </div>
  );
}
