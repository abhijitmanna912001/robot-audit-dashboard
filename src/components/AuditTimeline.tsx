import {
  CircleCheck,
  CircleHelp,
  CircleX,
  ClipboardList,
  Inbox,
  Play,
  Scale,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import type { AuditEvent } from "@/types/draft";
import { badgeVariants } from "@/components/ui/badge";
import { formatTime } from "@/lib/format";
import { getMarker, type Marker } from "@/lib/outcome";
import { readPlanFailure, text } from "@/lib/payload";
import { cn } from "@/lib/utils";
import { EventDetails } from "./EventDetails";

// Red = blocked (fail), amber = broke (error). Pass is green. These are the same status
// tokens as the badges: fail -> rejected (red), error -> failed (amber).
const markerStyles: Record<Marker, string> = {
  pass: "bg-status-completed text-status-completed-foreground",
  fail: "bg-status-rejected text-status-rejected-foreground",
  error: "bg-status-failed text-status-failed-foreground",
};

const nodeStyles: Record<Marker | "neutral", string> = {
  pass: "border-status-completed-accent text-status-completed-accent",
  fail: "border-status-rejected-accent text-status-rejected-accent",
  error: "border-status-failed-accent text-status-failed-accent",
  neutral: "border-border text-muted-foreground",
};

const icons: Record<string, LucideIcon> = {
  command_received: Inbox,
  plan_generated: ClipboardList,
  validator_result: ShieldCheck,
  critic_result: Scale,
  execution_started: Play,
  execution_completed: CircleCheck,
  execution_failed: CircleX,
};

// Node colour. Validator and critic come from getMarker (lib/outcome.ts); the rest are
// decided here, for display only.
function nodeTone(e: AuditEvent, marker: Marker | undefined): Marker | "neutral" {
  if (marker) return marker;
  if (e.eventType === "execution_completed") return "pass";
  if (e.eventType === "execution_failed") return "error";
  if (e.eventType === "plan_generated" && readPlanFailure(e.payload)) return "error";
  return "neutral";
}

// Events arrive already ordered oldest first (see lib/merge.ts).
// Server-safe on purpose: /dev-preview renders this from a server component, so it uses
// badgeVariants on a plain span rather than the Badge component.
export function AuditTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">No audit events to show.</p>;
  }

  return (
    <ol className="relative">
      {events.map((e, i) => {
        const marker = getMarker(e);
        const Icon = icons[e.eventType as string] ?? CircleHelp;
        const last = i === events.length - 1;
        return (
          <li key={e.id} className={cn("relative min-w-0 pl-11", last ? "pb-0" : "pb-6")}>
            {!last && (
              <span aria-hidden="true" className="absolute top-8 bottom-0 left-4 w-px bg-border" />
            )}
            <span
              aria-hidden="true"
              className={cn(
                "absolute top-0 left-0 flex size-8 items-center justify-center rounded-full border-2 bg-card",
                nodeStyles[nodeTone(e, marker)],
              )}
            >
              <Icon className="size-4" />
            </span>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 pt-1">
              <span className="wrap-anywhere font-mono text-xs font-medium">
                {text(e.eventType) ?? "unknown"}
              </span>
              {marker && (
                <span
                  className={cn(
                    badgeVariants(),
                    "h-auto px-2 py-0.5 text-[0.7rem] uppercase tracking-wide",
                    markerStyles[marker],
                  )}
                >
                  {marker}
                </span>
              )}
              <time className="text-xs text-muted-foreground" dateTime={e.createdAt}>
                {formatTime(e.createdAt)}
              </time>
            </div>
            <p className="wrap-anywhere mt-1 text-sm leading-relaxed">{text(e.message) ?? ""}</p>
            <EventDetails event={e} />
          </li>
        );
      })}
    </ol>
  );
}
