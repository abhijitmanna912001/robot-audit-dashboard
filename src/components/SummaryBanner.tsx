import { CircleCheck, Hourglass, Loader, OctagonX, TriangleAlert, type LucideIcon } from "lucide-react";
import type { BannerTone } from "@/lib/pipeline";
import { cn } from "@/lib/utils";

// success = done, blocked (red) = stopped by the validator or critic,
// broke (amber) = something failed, running = in progress, waiting = nothing yet.
const tones: Record<BannerTone, { icon: LucideIcon; box: string; iconColor: string }> = {
  success: {
    icon: CircleCheck,
    box: "border-status-completed-accent/40 bg-status-completed-accent/10",
    iconColor: "text-status-completed-accent",
  },
  blocked: {
    icon: OctagonX,
    box: "border-status-rejected-accent/60 bg-status-rejected-accent/15",
    iconColor: "text-status-rejected-accent",
  },
  broke: {
    icon: TriangleAlert,
    box: "border-status-failed-accent/60 bg-status-failed-accent/15",
    iconColor: "text-status-failed-accent",
  },
  running: {
    icon: Loader,
    box: "border-status-info-accent/50 bg-status-info-accent/10",
    iconColor: "text-status-info-accent",
  },
  waiting: {
    icon: Hourglass,
    box: "border-border bg-muted/40",
    iconColor: "text-muted-foreground",
  },
};

export function SummaryBanner({ text, tone }: { text: string; tone: BannerTone }) {
  const t = tones[tone];
  const Icon = t.icon;
  return (
    <div role="status" aria-live="polite" className={cn("flex items-center gap-3 rounded-lg border px-3 py-2", t.box)}>
      <Icon
        aria-hidden="true"
        className={cn("size-4 shrink-0", t.iconColor, tone === "running" && "animate-spin")}
      />
      <p className="wrap-anywhere min-w-0 text-sm font-medium leading-snug">{text}</p>
    </div>
  );
}
