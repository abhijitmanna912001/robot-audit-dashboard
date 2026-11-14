import { CircleCheck, Hourglass, Loader, OctagonX, TriangleAlert, type LucideIcon } from "lucide-react";
import type { BannerTone } from "@/lib/pipeline";
import { cn } from "@/lib/utils";

// Outcome colour for the whole card. Only status colours are used here, azure is not.
// success = done, blocked (red) = stopped by the validator or critic, broke (amber) = something
// failed, running = in progress, waiting = nothing yet.
const tones: Record<BannerTone, { icon: LucideIcon; box: string; ring: string; stamp: string }> = {
  success: {
    icon: CircleCheck,
    box: "border-l-status-completed-accent bg-status-completed",
    ring: "text-status-completed-accent",
    stamp: "text-status-completed-foreground",
  },
  blocked: {
    icon: OctagonX,
    box: "border-l-status-rejected-accent bg-status-rejected",
    ring: "text-status-rejected-accent",
    stamp: "text-status-rejected-foreground",
  },
  broke: {
    icon: TriangleAlert,
    box: "border-l-status-failed-accent bg-status-failed",
    ring: "text-status-failed-accent",
    stamp: "text-status-failed-foreground",
  },
  running: {
    icon: Loader,
    box: "border-l-status-info-accent bg-status-info",
    ring: "text-status-info-accent",
    stamp: "text-status-info-foreground",
  },
  waiting: {
    icon: Hourglass,
    box: "border-l-status-received-accent bg-muted",
    ring: "text-status-received-accent",
    stamp: "text-status-received-foreground",
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
      className={cn("anim-verdict grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-4 rounded-ui-sm border-l-3 p-5 @case:p-6", t.box)}
    >
      <div className="min-w-0">
        <p className={cn("font-mono text-xs leading-5 font-medium tracking-[0.1em] wrap-anywhere", t.stamp)}>
          {stamp}
        </p>
        <p className="mt-2 wrap-anywhere text-2xl leading-snug font-semibold tracking-tight text-balance">{headline}</p>
      </div>
      <span aria-hidden="true" className={cn("mt-1 flex size-8 shrink-0 items-center justify-center", t.ring)}>
        <Icon className={cn("size-7", tone === "running" && "animate-spin")} />
      </span>
      {facts.length > 0 && (
        <ul aria-label="Outcome facts" className="col-span-2 flex min-w-0 flex-wrap gap-x-5 gap-y-2 text-sm leading-5 text-muted-foreground tabular-nums">
          {facts.map((f) => <li key={f} className="min-w-0 wrap-anywhere">{f}</li>)}
        </ul>
      )}
    </div>
  );
}
