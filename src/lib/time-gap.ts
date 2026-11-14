// "+2.1 s" gap between two audit events, for the timeline. Pure, never throws.
// Undefined when either timestamp is invalid or the gap is negative (out of order).
const ms = (v: unknown): number => (typeof v === "string" ? Date.parse(v) : NaN);

export function formatGap(prevIso: unknown, iso: unknown): string | undefined {
  const a = ms(prevIso);
  const b = ms(iso);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return undefined;
  const d = b - a;
  if (d < 0) return undefined;
  if (d < 1000) return `+${Math.round(d)} ms`;
  if (d < 10_000) {
    const tenths = Math.round(d / 100) / 10;
    if (tenths < 10) return `+${tenths.toFixed(1)} s`;
  }
  const total = Math.round(d / 1000);
  if (total < 60) return `+${total} s`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return s === 0 ? `+${m} min` : `+${m} min ${s} s`;
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return m === 0 ? `+${h} h` : `+${h} h ${m} min`;
}
