"use client";

import { useEffect, useState } from "react";
import { formatDateTime, formatLocal, formatTime, formatRelative } from "@/lib/format";

// Server and first client render both show the UTC fallback, so the HTML matches and there
// is no hydration mismatch. After mount the viewer's local time replaces it ("HH:mm:ss", with
// the date in front when `showDate` is set and the day is not today).
// The title always carries the full UTC timestamp.
export function LocalTime({
  iso,
  showDate = false,
  showRelative = false,
  className,
}: {
  iso: string;
  showDate?: boolean;
  showRelative?: boolean;
  className?: string;
}) {
  const [display, setDisplay] = useState<{ local: string | undefined; relative: string | undefined } | null>(null);

  useEffect(() => {
    const now = new Date();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional post-mount switch from the UTC fallback
    setDisplay({ local: formatLocal(iso, now, { withDate: showDate }), relative: showRelative ? formatRelative(iso, now) : undefined });
  }, [iso, showDate, showRelative]);

  return (
    <time dateTime={iso} title={`${formatDateTime(iso)}${display?.relative ? ` · ${display.relative}` : ""}`} className={`tabular-nums ${className ?? ""}`}>
      {display?.local ?? formatTime(iso)}
    </time>
  );
}
