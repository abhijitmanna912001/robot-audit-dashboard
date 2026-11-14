"use client";

import { useEffect, useState } from "react";
import { formatDateTime, formatTime } from "@/lib/format";

const pad = (n: number) => String(n).padStart(2, "0");

// Server and first client render both show the UTC fallback, so the HTML matches and there
// is no hydration mismatch. After mount the viewer's local HH:mm:ss replaces it.
// The title always carries the full UTC timestamp.
export function LocalTime({ iso, className }: { iso: string; className?: string }) {
  const [local, setLocal] = useState<string | null>(null);

  useEffect(() => {
    const d = new Date(iso);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional post-mount switch from the UTC fallback
    setLocal(Number.isNaN(d.getTime()) ? null : `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
  }, [iso]);

  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={`tabular-nums ${className ?? ""}`}>
      {local ?? formatTime(iso)}
    </time>
  );
}
