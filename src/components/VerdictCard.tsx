import { CircleCheck, Hourglass, Loader, OctagonX, TriangleAlert, type LucideIcon } from "lucide-react";
import type { BannerTone } from "@/lib/pipeline";
import { cn } from "@/lib/utils";

// Outcome colour for the whole card. Only status colours are used here, azure is not.
// success = done, blocked (red) = stopped by the validator or critic, broke (amber) = something
// failed, running = in progress, waiting = nothing yet.
const tones: Record<BannerTone, { icon: LucideIcon; box: string; ring: string; stamp: string }> = {
  success: {
    icon: CircleCheck,
    box: "border-status-completed-accent/40 bg-linear-to-b from-status-completed-accent/15 to-status-completed-accent/5",
    ring: "border-status-completed-accent/60 text-status-completed-accent shadow-[0_0_24px_-2px_var(--status-completed-accent)]",
    stamp: "border-status-completed-accent/50 text-status-completed-accent",
  },
  blocked: {
    icon: OctagonX,
    box: "border-status-rejected-accent/50 bg-linear-to-b from-status-rejected-accent/18 to-status-rejected-accent/6",
    ring: "border-status-rejected-accent/70 text-status-rejected-accent shadow-[0_0_24px_-2px_var(--status-rejected-accent)]",
    stamp: "border-status-rejected-accent/60 text-status-rejected-accent",
  },
  broke: {
    icon: TriangleAlert,
    box: "border-status-failed-accent/50 bg-linear-to-b from-status-failed-accent/18 to-status-failed-accent/6",
    ring: "border-status-failed-accent/70 text-status-failed-accent shadow-[0_0_24px_-2px_var(--status-failed-accent)]",
    stamp: "border-status-failed-accent/60 text-status-failed-accent",
  },
  running: {
    icon: Loader,
    box: "border-status-info-accent/40 bg-linear-to-b from-status-info-accent/15 to-status-info-accent/5",
    ring: "border-status-info-accent/60 text-status-info-accent shadow-[0_0_24px_-2px_var(--status-info-accent)]",
    stamp: "border-status-info-accent/50 text-status-info-accent",
  },
  waiting: {
    icon: Hourglass,
    box: "border-border bg-linear-to-b from-raised to-surface",
    ring: "border-border text-muted-foreground",
    stamp: "border-border text-muted-foreground",
  },
};

// The outcome of the selected command: the exact summarize() text as the headline, a facts line
// built only from pieces that exist, and a mono stamp. Server-safe (no hooks).
export function VerdictCard({
  headline,
  facts,
  stamp,
  tone,
}: {
  headline: string;
  facts: string[];
  stamp: string;
  tone: BannerTone;
}) {
  const t = tones[tone];
  const Icon = t.icon;
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn("anim-verdict flex flex-wrap items-center gap-x-4 gap-y-3 rounded-ui-lg border p-4", t.box)}
    >
      <span
        aria-hidden="true"
        className={cn("flex size-12 shrink-0 items-center justify-center rounded-full border-2 bg-background/40", t.ring)}
      >
        <Icon className={cn("size-6", tone === "running" && "animate-spin")} />
      </span>
      <div className="min-w-0 flex-1 basis-56">
        <p className="wrap-anywhere text-xl leading-tight font-semibold text-balance">{headline}</p>
        {facts.length > 0 && (
          // Each fact is one unbreakable unit (its numbers are already bound to their units with
          // non-breaking spaces), so a line only wraps between facts.
          <p className="mt-1 flex flex-wrap gap-x-2 text-[13px] leading-5 text-muted-foreground tabular-nums">
            {facts.map((f, i) => (
              <span key={f} className="whitespace-nowrap">
                {i > 0 && (
                  <span aria-hidden="true" className="mr-2">
                    ·
                  </span>
                )}
                {f}
              </span>
            ))}
          </p>
        )}
      </div>
      <span
        className={cn(
          "shrink-0 rounded-full border px-3 py-1 font-mono text-xs leading-4 font-medium tracking-[0.14em] whitespace-nowrap",
          t.stamp,
        )}
      >
        {stamp}
      </span>
    </div>
  );
}
