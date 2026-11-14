import type { AuditEvent, Command } from "@/types/draft";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { stageDetails, verdictFacts, verdictStamp } from "@/lib/verdict";
import { AuditTimeline } from "./AuditTimeline";
import { LocalTime } from "./LocalTime";
import { PipelineStrip } from "./PipelineStrip";
import { VerdictCard } from "./VerdictCard";

export const sectionLabel = "font-mono font-medium text-xs leading-4 uppercase tracking-[0.12em] text-muted-foreground";

// The selected command as a case file: header, verdict card, verification chain, timeline.
// Server-safe (no hooks) so /dev-preview renders the same layout from a server component.
export function CaseFile({
  command,
  events,
  highlightIds,
}: {
  command: Pick<Command, "id" | "text" | "status" | "createdAt">;
  events: AuditEvent[];
  highlightIds?: ReadonlySet<string>;
}) {
  const stamp = verdictStamp(command.status, events);
  return (
    // @container: the layout inside responds to this column's width (not the window), so the
    // /dev-preview phone frame shows the real mobile layout.
    <article className="@container surface-card min-w-0 rounded-ui-lg" aria-label="Case file">
      <div className="sticky-case sticky z-10 flex min-w-0 items-center gap-3 rounded-t-ui-lg border-b border-border bg-surface px-5 py-3 @case:px-8">
        <p className="min-w-0 flex-1 truncate text-sm font-medium" title={command.text}>
          {command.text}
        </p>
        <span className="shrink-0 rounded-ui-sm border border-border px-2 py-1 font-mono text-xs leading-4 tracking-wide text-muted-foreground">
          {stamp}
        </span>
        <span className="hidden shrink-0 font-mono text-xs text-muted-foreground @case:inline">
          {command.id.slice(0, 8)}
        </span>
      </div>
      <div className="flex min-w-0 flex-col gap-6 p-5 @case:p-8">
        <header className="min-w-0">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className={sectionLabel}>Command audit</h2>
            <span className="text-xs text-muted-foreground">Append-only record</span>
          </div>
          {/* Keep the full command available when a long command is visually clamped. */}
          <p
            className="mt-5 line-clamp-3 wrap-anywhere text-[32px] leading-[1.18] font-medium tracking-[-0.035em] text-balance @case:text-4xl"
            title={command.text}
          >
            {command.text}
          </p>
          <p className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 font-mono text-xs leading-4 text-muted-foreground">
            <span title={command.id}>ID / {command.id.slice(0, 8)}</span>
            <LocalTime iso={command.createdAt} showDate />
          </p>
        </header>
        <VerdictCard
          headline={summarize(command.status, events)}
          facts={verdictFacts(events)}
          stamp={stamp}
          tone={bannerTone(command.status, events)}
        />
        <section aria-label="Verification chain" className="border-b border-border pb-6">
          <h3 className={`${sectionLabel} mb-5`}>Verification chain</h3>
          <PipelineStrip stages={derivePipeline(events)} details={stageDetails(events)} />
        </section>
        <section aria-label="Audit timeline" className="min-w-0">
          <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
            <h3 className={sectionLabel}>Audit timeline</h3>
            <span className="font-mono text-xs text-muted-foreground">{events.length} {events.length === 1 ? "event" : "events"}</span>
          </div>
          <AuditTimeline events={events} highlightIds={highlightIds} />
        </section>
      </div>
    </article>
  );
}
