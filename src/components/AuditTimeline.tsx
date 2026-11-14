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
  pass: "border-status-completed-accent bg-status-completed text-status-completed-foreground",
  fail: "border-status-rejected-accent bg-status-rejected text-status-rejected-foreground",
  error: "border-status-failed-accent bg-status-failed text-status-failed-foreground",
};

// Rail segment below a node, coloured by that event's outcome.
const railStyles: Record<Marker | "neutral", string> = {
  pass: "bg-status-completed-accent",
  fail: "bg-status-rejected-accent",
  error: "bg-status-failed-accent",
  neutral: "bg-border",
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
export function AuditTimeline({
  events,
  highlightIds,
}: {
  events: AuditEvent[];
  highlightIds?: ReadonlySet<string>;
}) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">No audit events recorded yet. This record will update as events arrive.</p>;
  }

  const ctx = eventContext(events);
  return (
    <ol className="anim-timeline relative">
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
            className={cn(
              "relative grid min-w-0 grid-cols-[1.75rem_minmax(0,1fr)] gap-x-3 rounded-ui-sm @case:grid-cols-[max-content_1.75rem_minmax(0,1fr)] @case:gap-x-4",
              last ? "pb-0" : "pb-7",
              highlightIds?.has(e.id) && "event-flash",
            )}
          >
            <div className="col-start-2 mb-2 flex min-w-24 flex-wrap items-center gap-x-3 font-mono text-xs leading-4 text-muted-foreground @case:col-start-1 @case:row-start-1 @case:mb-0 @case:block @case:pt-1 @case:text-right">
              <LocalTime iso={e.createdAt} className="block" />
              {gap && <span className="block whitespace-nowrap tabular-nums @case:mt-1">{gap}</span>}
            </div>
            <div className="relative col-start-1 row-span-2 row-start-1 flex justify-center @case:col-start-2 @case:row-span-1">
              {!last && (
                <span
                  aria-hidden="true"
                  className={cn("absolute top-8 -bottom-7 w-px", railStyles[tone])}
                />
              )}
              <span
                aria-hidden="true"
                className={cn(
                  "relative flex size-7 items-center justify-center rounded-full border bg-surface",
                  nodeStyles[tone],
                )}
              >
                <Icon className="size-3.5" />
              </span>
            </div>
            <div className="col-start-2 min-w-0 @case:col-start-3 @case:row-start-1">
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 pt-1">
                <p className="wrap-anywhere min-w-0 flex-1 basis-40 text-sm leading-5 font-semibold">
                  {title}
                </p>
                {marker && (
                  <span
                    className={cn(
                      badgeVariants(),
                      "h-auto rounded-ui-sm! px-2 py-1 font-mono text-xs leading-none uppercase tracking-wide",
                      markerStyles[marker],
                    )}
                  >
                    {marker}
                  </span>
                )}
              </div>
              {summary && hasLine && (
                <p className="wrap-anywhere mt-1.5 flex flex-wrap items-center gap-x-2 text-sm leading-5 text-muted-foreground">
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
