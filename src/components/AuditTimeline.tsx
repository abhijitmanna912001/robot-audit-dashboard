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
import { eventContext, isRedundant, summarizeEvent } from "@/lib/event-summary";
import { getMarker, type Marker } from "@/lib/outcome";
import { readPlanFailure, text } from "@/lib/payload";
import { formatGap } from "@/lib/time-gap";
import { cn } from "@/lib/utils";
import { CopyButton } from "./CopyButton";
import { EventDetails } from "./EventDetails";
import { LocalTime } from "./LocalTime";

// Red = blocked (fail), amber = broke (error). Pass is green. These are the same status
// tokens as the badges: fail -> rejected (red), error -> failed (amber).
const markerStyles: Record<Marker, string> = {
  pass: "bg-status-completed text-status-completed-foreground",
  fail: "bg-status-rejected text-status-rejected-foreground",
  error: "bg-status-failed text-status-failed-foreground",
};

// Rail segment below a node, coloured by that event's outcome.
const railStyles: Record<Marker | "neutral", string> = {
  pass: "bg-status-completed-accent/60",
  fail: "bg-status-rejected-accent/60",
  error: "bg-status-failed-accent/60",
  neutral: "bg-border",
};

const nodeStyles: Record<Marker | "neutral", string> = {
  pass: "border-status-completed-accent text-status-completed-accent shadow-[0_0_12px_-2px_var(--status-completed-accent)]",
  fail: "border-status-rejected-accent text-status-rejected-accent shadow-[0_0_12px_-2px_var(--status-rejected-accent)]",
  error: "border-status-failed-accent text-status-failed-accent shadow-[0_0_12px_-2px_var(--status-failed-accent)]",
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
export function AuditTimeline({
  events,
  highlightIds,
}: {
  events: AuditEvent[];
  highlightIds?: ReadonlySet<string>;
}) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">No audit events to show.</p>;
  }

  const ctx = eventContext(events);
  return (
    <ol className="relative">
      {events.map((e, i) => {
        const marker = getMarker(e);
        const Icon = icons[e.eventType as string] ?? CircleHelp;
        const last = i === events.length - 1;
        const title = text(e.message) ?? text(e.eventType) ?? "Unknown event";
        const summary = summarizeEvent(e, ctx);
        // Hide text that only repeats the title; a hash is always new information.
        const lineText = summary && !isRedundant(title, summary.text) ? summary.text : "";
        const tone = nodeTone(e, marker);
        const gap = i > 0 ? formatGap(events[i - 1].createdAt, e.createdAt) : undefined;
        const hasLine = lineText !== "" || summary?.shortHash !== undefined;
        return (
          <li
            key={e.id}
            style={{ "--i": i } as React.CSSProperties}
            className={cn(
              "anim-row relative grid min-w-0 grid-cols-[4.25rem_1.75rem_minmax(0,1fr)] gap-x-2 rounded-ui-md sm:grid-cols-[5rem_1.75rem_minmax(0,1fr)] sm:gap-x-3",
              last ? "pb-0" : "pb-6",
              highlightIds?.has(e.id) && "event-flash",
            )}
          >
            <div className="pt-1 text-right font-mono text-xs leading-tight text-muted-foreground">
              <LocalTime iso={e.createdAt} className="block" />
              {gap && <span className="mt-0.5 block tabular-nums">{gap}</span>}
            </div>
            <div className="relative flex justify-center">
              {!last && (
                <span
                  aria-hidden="true"
                  className={cn("absolute top-8 -bottom-6 w-px", railStyles[tone])}
                />
              )}
              <span
                aria-hidden="true"
                className={cn(
                  "relative flex size-7 items-center justify-center rounded-full border-2 bg-raised",
                  nodeStyles[tone],
                )}
              >
                <Icon className="size-3.5" />
              </span>
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 pt-0.5">
                <p className="wrap-anywhere min-w-0 flex-1 basis-40 text-sm leading-snug font-medium text-balance">
                  {title}
                </p>
                {marker && (
                  <span
                    className={cn(
                      badgeVariants(),
                      "h-auto rounded-full! px-2 py-px font-mono text-[11px] uppercase tracking-wide",
                      markerStyles[marker],
                    )}
                  >
                    {marker}
                  </span>
                )}
              </div>
              {summary && hasLine && (
                <p className="wrap-anywhere mt-0.5 flex flex-wrap items-center gap-x-1 text-sm text-muted-foreground">
                  {lineText && <span>{lineText}</span>}
                  {summary.shortHash && summary.hash && (
                    <span className="inline-flex items-center gap-0.5">
                      {lineText && <span aria-hidden="true">·</span>}
                      <span className="font-mono text-xs" title={summary.hash}>
                        #{summary.shortHash}
                      </span>
                      <CopyButton value={summary.hash} label="plan hash" />
                    </span>
                  )}
                </p>
              )}
              <EventDetails event={e} />
            </div>
          </li>
        );
      })}
    </ol>
  );
}
