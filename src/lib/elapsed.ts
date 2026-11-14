// Display-only processing and queue durations, derived from audit timestamps. Pure.
type EventLike = { eventType?: unknown; createdAt?: unknown };
const timestamp = (event: EventLike | null | undefined): number =>
  typeof event?.createdAt === "string" ? Date.parse(event.createdAt) : NaN;

function planTime(events: readonly EventLike[]): number | undefined {
  const times = events.filter((e) => e?.eventType === "plan_generated").map(timestamp).filter(Number.isFinite);
  return times.length > 0 ? Math.min(...times) : undefined;
}

// Processing starts at the earliest valid plan, including a recorded planner failure.
// Retries do not reset the clock. Without a plan or a later sample, do not invent a duration.
export function elapsedMs(events: readonly EventLike[]): number | undefined {
  if (!Array.isArray(events)) return undefined;
  const start = planTime(events);
  if (start === undefined) return undefined;
  const times = events.map(timestamp).filter((t) => Number.isFinite(t) && t >= start);
  return times.length >= 2 ? Math.max(...times) - start : undefined;
}

// A completed queue wait needs both a receipt and a plan. No live clock is inferred.
export function workerWaitMs(events: readonly EventLike[]): number | undefined {
  if (!Array.isArray(events)) return undefined;
  const end = planTime(events);
  if (end === undefined) return undefined;
  const received = events.filter((e) => e?.eventType === "command_received").map(timestamp).filter(Number.isFinite);
  if (received.length === 0) return undefined;
  const wait = end - Math.min(...received);
  return wait >= 0 ? wait : undefined;
}

export function workerWaitLabel(events: readonly EventLike[]): string | undefined {
  const ms = workerWaitMs(events);
  if (ms === undefined || ms <= 2000) return undefined;
  return `Waited ${formatElapsed(ms)} for the worker`;
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
