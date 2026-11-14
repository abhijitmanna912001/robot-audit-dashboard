"use client";

import type { Tab } from "@/lib/command-filter";
import { cn } from "@/lib/utils";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "attention", label: "Needs attention" },
  { id: "completed", label: "Completed" },
];

// Client-side filters only. The three filters are toggle buttons over one list (not a tablist),
// drawn as a small segmented control.
export function FilterTabs({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  return (
    <div role="group" aria-label="Filter commands" className="flex w-full gap-0.5 rounded-ui-sm border border-border bg-raised p-0.5">
      {TABS.map((t) => (
        <button
          key={t.id}
          type="button"
          aria-pressed={tab === t.id}
          onClick={() => onTab(t.id)}
          className={cn(
            "min-h-8 min-w-0 flex-1 truncate rounded-[4px] px-2 text-xs leading-4 outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/30",
            tab === t.id
              ? "bg-surface font-medium text-primary shadow-xs"
              : "text-muted-foreground hover:text-foreground active:bg-muted",
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

// The test-commands toggle is a switch with the hidden count in its label.
export function TestSwitch({
  showTest,
  onShowTest,
  hiddenTestCount,
}: {
  showTest: boolean;
  onShowTest: () => void;
  hiddenTestCount: number;
}) {
  const label = !showTest && hiddenTestCount > 0 ? `Test commands (${hiddenTestCount})` : "Test commands";
  return (
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
  );
}
