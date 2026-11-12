import type { AuditEvent } from "@/types/draft";

export function AuditTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-zinc-500">No audit events to show.</p>;
  }

  const sorted = [...events].sort((a, b) => a.timestamp.localeCompare(b.timestamp));

  return (
    <ol className="ml-2 border-l border-zinc-300 dark:border-zinc-700">
      {sorted.map((e) => (
        <li key={e.id} className="relative pb-4 pl-5 last:pb-0">
          <span className="absolute -left-1.5 top-1.5 h-3 w-3 rounded-full bg-zinc-400 dark:bg-zinc-500" />
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-xs font-medium">{e.eventType}</span>
            <time className="text-xs text-zinc-500" dateTime={e.timestamp}>
              {new Date(e.timestamp).toISOString().slice(11, 19)} UTC
            </time>
          </div>
          <p className="text-sm">{e.message}</p>
        </li>
      ))}
    </ol>
  );
}
