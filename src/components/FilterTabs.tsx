"use client";

import { Button } from "@/components/ui/button";
import type { Tab } from "@/lib/command-filter";
import { cn } from "@/lib/utils";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "attention", label: "Needs attention" },
  { id: "completed", label: "Completed" },
];

// Client-side filters only. They are toggle buttons over one list, not a tablist.
export function FilterTabs({
  tab,
  onTab,
  showTest,
  onShowTest,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  showTest: boolean;
  onShowTest: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
      <div role="group" aria-label="Filter commands" className="flex flex-wrap gap-1">
        {TABS.map((t) => (
          <Button
            key={t.id}
            type="button"
            variant="ghost"
            size="sm"
            aria-pressed={tab === t.id}
            onClick={() => onTab(t.id)}
            className={cn(
              "text-muted-foreground transition-colors duration-150",
              tab === t.id && "bg-primary/15 text-foreground",
            )}
          >
            {t.label}
          </Button>
        ))}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="xs"
        aria-pressed={showTest}
        onClick={onShowTest}
        className="text-muted-foreground transition-colors duration-150"
      >
        {showTest ? "Hide test commands" : "Show test commands"}
      </Button>
    </div>
  );
}
