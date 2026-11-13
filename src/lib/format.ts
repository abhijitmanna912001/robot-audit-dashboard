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
