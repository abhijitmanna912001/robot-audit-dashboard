import type { AuditEvent } from "@/types/draft";
import { formatTime } from "@/lib/format";
import { getMarker, type Marker } from "@/lib/outcome";
import { text } from "@/lib/payload";
import { EventDetails } from "./EventDetails";

const markerStyles: Record<Marker, string> = {
  pass: "bg-green-200 text-green-900 dark:bg-green-900 dark:text-green-100",
  fail: "bg-orange-200 text-orange-900 dark:bg-orange-900 dark:text-orange-100",
  error: "bg-red-600 text-white dark:bg-red-700",
};

// Events arrive already ordered oldest first (see lib/merge.ts).
export function AuditTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-zinc-500">No audit events to show.</p>;
  }

  return (
    <ol className="ml-2 border-l border-zinc-300 dark:border-zinc-700">
      {events.map((e) => {
        const marker = getMarker(e);
        return (
          <li key={e.id} className="relative min-w-0 pb-4 pl-5 last:pb-0">
            <span className="absolute -left-1.5 top-1.5 h-3 w-3 rounded-full bg-zinc-400 dark:bg-zinc-500" />
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="wrap-anywhere font-mono text-xs font-medium">{text(e.eventType) ?? "unknown"}</span>
              {marker && (
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium uppercase ${markerStyles[marker]}`}
                >
                  {marker}
                </span>
              )}
              <time className="text-xs text-zinc-500" dateTime={e.createdAt}>
                {formatTime(e.createdAt)}
              </time>
            </div>
            <p className="wrap-anywhere text-sm">{text(e.message) ?? ""}</p>
            <EventDetails event={e} />
          </li>
        );
      })}
    </ol>
  );
}
