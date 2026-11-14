import type { AuditEvent } from "@/types/draft";
import { badgeVariants } from "@/components/ui/badge";
import { formatTime } from "@/lib/format";
import { getMarker, type Marker } from "@/lib/outcome";
import { text } from "@/lib/payload";
import { cn } from "@/lib/utils";
import { EventDetails } from "./EventDetails";

// Pass, fail and error reuse the status tokens: completed, rejected and failed.
const markerStyles: Record<Marker, string> = {
  pass: "bg-status-completed text-status-completed-foreground",
  fail: "bg-status-rejected text-status-rejected-foreground",
  error: "bg-status-failed text-status-failed-foreground",
};

// Events arrive already ordered oldest first (see lib/merge.ts).
// Server-safe on purpose: /dev-preview renders this from a server component, so it uses
// badgeVariants on a plain span rather than the Badge component.
export function AuditTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">No audit events to show.</p>;
  }

  return (
    <ol className="ml-2 border-l border-border">
      {events.map((e) => {
        const marker = getMarker(e);
        return (
          <li key={e.id} className="relative min-w-0 pb-5 pl-5 last:pb-0">
            <span className="absolute -left-1.5 top-1.5 size-3 rounded-full border-2 border-background bg-muted-foreground" />
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="wrap-anywhere font-mono text-xs font-medium">
                {text(e.eventType) ?? "unknown"}
              </span>
              {marker && (
                <span
                  className={cn(
                    badgeVariants(),
                    "h-auto px-2 py-0.5 text-[0.7rem] uppercase tracking-wide",
                    markerStyles[marker],
                  )}
                >
                  {marker}
                </span>
              )}
              <time className="text-xs text-muted-foreground" dateTime={e.createdAt}>
                {formatTime(e.createdAt)}
              </time>
            </div>
            <p className="wrap-anywhere mt-1 text-sm leading-relaxed">{text(e.message) ?? ""}</p>
            <EventDetails event={e} />
          </li>
        );
      })}
    </ol>
  );
}
