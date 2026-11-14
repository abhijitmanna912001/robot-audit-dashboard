import type { AuditEvent, Command } from "@/types/draft";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { stageDetails, verdictFacts, verdictStamp } from "@/lib/verdict";
import { AuditTimeline } from "./AuditTimeline";
import { LocalTime } from "./LocalTime";
import { PipelineStrip } from "./PipelineStrip";
import { VerdictCard } from "./VerdictCard";

export const sectionLabel = "font-medium text-[11px] leading-4 uppercase tracking-[0.14em] text-muted-foreground";

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
    <article className="@container flex min-w-0 flex-col gap-4" aria-label="Case file">
      {/* Compact sticky bar. Quiet while the full title block is in view (see .sticky-case). */}
      <div className="sticky-case sticky top-0 z-10 -mx-1 flex h-10 items-center gap-3 border-b border-border bg-background/90 px-4 backdrop-blur">
        <p className="min-w-0 flex-1 truncate text-sm leading-5 font-medium" title={command.text}>
          {command.text}
        </p>
        <span className="shrink-0 rounded-full border border-border px-2 font-mono text-[11px] leading-4 tracking-[0.14em] whitespace-nowrap text-muted-foreground">
          {stamp}
        </span>
        <span className="hidden shrink-0 font-mono text-xs leading-4 text-muted-foreground @case:inline">
          {command.id.slice(0, 8)}
        </span>
      </div>
      <header className="min-w-0">
        <h2 className={sectionLabel}>Case file</h2>
        {/* The full text lives only in this element's title, so no tooltip covers the verdict. */}
        <p
          className="mt-2 line-clamp-2 wrap-anywhere text-[28px] leading-tight font-semibold tracking-tight text-balance"
          title={command.text}
        >
          {command.text}
        </p>
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs leading-4 text-muted-foreground">
          <span>{command.id.slice(0, 8)}</span>
          <LocalTime iso={command.createdAt} showDate />
        </p>
      </header>
      <VerdictCard
        headline={summarize(command.status, events)}
        facts={verdictFacts(events)}
        stamp={stamp}
        tone={bannerTone(command.status, events)}
      />
      <PipelineStrip stages={derivePipeline(events)} details={stageDetails(events)} />
      <section aria-label="Audit timeline" className="surface-card min-w-0 rounded-ui-lg p-4">
        <h3 className={`${sectionLabel} mb-4`}>Audit timeline</h3>
        <AuditTimeline events={events} highlightIds={highlightIds} />
      </section>
    </article>
  );
}
