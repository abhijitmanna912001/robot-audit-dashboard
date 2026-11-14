"use client";

import { useEffect, useState } from "react";
import { formatDateTime, formatLocal, formatTime } from "@/lib/format";

// Server and first client render both show the UTC fallback, so the HTML matches and there
// is no hydration mismatch. After mount the viewer's local time replaces it ("HH:mm:ss", with
// the date in front when `showDate` is set and the day is not today).
// The title always carries the full UTC timestamp.
export function LocalTime({
  iso,
  showDate = false,
  className,
}: {
  iso: string;
  showDate?: boolean;
  className?: string;
}) {
  const [local, setLocal] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional post-mount switch from the UTC fallback
    setLocal(formatLocal(iso, new Date(), { withDate: showDate }) ?? null);
  }, [iso, showDate]);

  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={`tabular-nums ${className ?? ""}`}>
      {local ?? formatTime(iso)}
    </time>
  );
}
