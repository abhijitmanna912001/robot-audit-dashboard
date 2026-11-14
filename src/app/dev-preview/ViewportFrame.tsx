"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";

type View = "desktop" | "phone";

// Right pane of /dev-preview: a small toolbar and a scroll area. The Desktop/Phone buttons only
// change the container width (full width, or a 390px centred frame), so the mobile layout can be
// checked without resizing the window. The case file is passed in as children and stays a server
// component; inside it the layout follows this container's width (@case:), not the window.
export function ViewportFrame({ title, children }: { title: string; children: React.ReactNode }) {
  const [view, setView] = useState<View>("desktop");
  const btn = (v: View, label: string) => (
    <button
      type="button"
      aria-pressed={view === v}
      onClick={() => setView(v)}
      className={cn(
        "h-9 rounded-ui-sm px-3 text-xs leading-4 outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/30",
        view === v
          ? "bg-accent text-primary hover:bg-primary/15"
          : "text-muted-foreground hover:bg-raised hover:text-foreground active:bg-muted",
      )}
    >
      {label}
    </button>
  );
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-2">
        <p className="min-w-0 truncate text-sm leading-5 text-muted-foreground">{title}</p>
        <div role="group" aria-label="Preview width" className="flex shrink-0 gap-1">
          {btn("desktop", "Desktop")}
          {btn("phone", "Phone")}
        </div>
      </div>
      <div
        id="scenario-panel"
        role="tabpanel"
        className="case-scrollport scroll-subtle min-h-0 min-w-0 flex-1 overflow-y-auto px-3 pb-3 [scrollbar-gutter:stable] sm:px-6 sm:pb-6"
      >
        <div
          className={cn(
            "mx-auto min-w-0",
            view === "phone" ? "w-[390px] max-w-full" : "w-full max-w-5xl",
          )}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
