import { elapsedLabel, workerWaitLabel } from "@/lib/elapsed";
import type { AuditEvent, Command } from "@/types/draft";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { stageDetails, verdictFacts, verdictStamp } from "@/lib/verdict";
import { AuditTimeline } from "./AuditTimeline";
import { LocalTime } from "./LocalTime";
import { PipelineStrip } from "./PipelineStrip";
import { VerdictCard } from "./VerdictCard";
import { CaseFileFrame } from "./CaseFileFrame";

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
  const processing = elapsedLabel(command.status, events);
  const wait = workerWaitLabel(events);
  return (
    <CaseFileFrame
      compactHeader={
        <div className="case-compact-header">
          <div className="flex min-w-0 items-start gap-3">
            <p className="min-w-0 flex-1 truncate text-sm font-medium" title={command.text}>{command.text}</p>
            <span className="shrink-0 rounded-ui-sm border border-border px-2 py-1 font-mono text-xs leading-4 text-muted-foreground">{stamp}</span>
          </div>
          <p className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs leading-5 text-muted-foreground">
            <span>Run {command.id.slice(0, 8)}</span>
            {processing && <span>{processing}</span>}
            {wait && <span>{wait}</span>}
          </p>
        </div>
      }
      titleBlock={
        <header className="min-w-0">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className={sectionLabel}>Command audit</h2>
            <span className="text-xs text-muted-foreground">Append-only record</span>
          </div>
          <p className="mt-5 line-clamp-3 wrap-anywhere text-[32px] leading-[1.18] font-medium tracking-[-0.035em] text-balance @case:text-4xl" title={command.text}>
            {command.text}
          </p>
          <p className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 font-mono text-xs leading-4 text-muted-foreground">
            <span title={command.id}>Run / {command.id.slice(0, 8)}</span>
            <LocalTime iso={command.createdAt} showDate />
          </p>
        </header>
      }
    >
      <VerdictCard headline={summarize(command.status, events)} facts={verdictFacts(events)} stamp={stamp} tone={bannerTone(command.status, events)} />
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
    </CaseFileFrame>
  );
}
