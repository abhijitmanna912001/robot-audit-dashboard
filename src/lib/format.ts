// Safe date formatting: a bad timestamp renders a fallback instead of throwing.
const FALLBACK = "unknown time";

export function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? FALLBACK : `${d.toISOString().slice(11, 19)} UTC`;
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? FALLBACK
    : `${d.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

// Local display time for the viewer: "HH:mm:ss" (24 hour), with the date in front when
// `withDate` is set and the day is not today ("13 Nov, 09:15:02", plus the year when it is
// another year). `timeZone` and `locale` can be injected, which keeps tests machine independent.
export function formatLocal(
  iso: string,
  now: Date,
  opts: { withDate?: boolean; timeZone?: string; locale?: string } = {},
): string | undefined {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return undefined;
  const { timeZone, locale } = opts;
  const time = new Intl.DateTimeFormat(locale, {
    timeZone,
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(d);
  if (!opts.withDate || Number.isNaN(now.getTime())) return time;
  const day = (x: Date) =>
    new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(x);
  if (day(d) === day(now)) return time;
  const sameYear = day(d).slice(0, 4) === day(now).slice(0, 4);
  const date = new Intl.DateTimeFormat(locale, {
    timeZone,
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(d);
  return `${date}, ${time}`;
}
