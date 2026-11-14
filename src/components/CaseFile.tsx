import type { AuditEvent, Command } from "@/types/draft";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { stageDetails, verdictFacts, verdictStamp } from "@/lib/verdict";
import { AuditTimeline } from "./AuditTimeline";
import { LocalTime } from "./LocalTime";
import { PipelineStrip } from "./PipelineStrip";
import { VerdictCard } from "./VerdictCard";

export const sectionLabel = "font-medium text-[11px] uppercase tracking-[0.14em] text-muted-foreground";

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
  return (
    <article className="flex min-w-0 flex-col gap-4" aria-label="Case file">
      <header className="min-w-0">
        <h2 className={sectionLabel}>Case file</h2>
        {/* The full text lives only in this element's title, so no tooltip covers the verdict. */}
        <p
          className="mt-2 line-clamp-2 wrap-anywhere text-[28px] leading-tight font-semibold tracking-tight text-balance"
          title={command.text}
        >
          {command.text}
        </p>
        <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-xs text-muted-foreground">
          <span>{command.id.slice(0, 8)}</span>
          <LocalTime iso={command.createdAt} showDate />
        </p>
      </header>
      <VerdictCard
        headline={summarize(command.status, events)}
        facts={verdictFacts(events)}
        stamp={verdictStamp(command.status, events)}
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
