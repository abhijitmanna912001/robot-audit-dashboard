// Elapsed time of a command, from its first to its last audit event. Pure.
type EventLike = { createdAt?: unknown };

// Milliseconds between the earliest and latest valid timestamp. Needs two valid events.
export function elapsedMs(events: readonly EventLike[]): number | undefined {
  if (!Array.isArray(events)) return undefined;
  const times: number[] = [];
  for (const e of events) {
    const t = typeof e?.createdAt === "string" ? Date.parse(e.createdAt) : NaN;
    if (Number.isFinite(t)) times.push(t);
  }
  if (times.length < 2) return undefined;
  return Math.max(...times) - Math.min(...times);
}

// "under 1 s", "41 s", "2 min 5 s", "2 min", "1 h 3 min", "1 h".
export function formatElapsed(ms: number): string | undefined {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return undefined;
  const total = Math.round(ms / 1000);
  if (total < 1) return "under 1 s";
  if (total < 60) return `${total} s`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return s === 0 ? `${m} min` : `${m} min ${s} s`;
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

// The wording depends on how the command ended: "Completed in" would be wrong for a rejection.
// Any other status is only the span to the last event, not a live clock.
export function elapsedLabel(status: string | undefined, events: readonly EventLike[]): string | undefined {
  const ms = elapsedMs(events);
  const span = ms === undefined ? undefined : formatElapsed(ms);
  if (span === undefined) return undefined;
  switch (status) {
    case "completed":
      return `Completed in ${span}`;
    case "rejected":
      return `Rejected after ${span}`;
    case "failed":
      return `Failed after ${span}`;
    default:
      return `${span} so far`;
  }
}
