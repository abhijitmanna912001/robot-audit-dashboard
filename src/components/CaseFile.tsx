import type { AuditEvent, Command } from "@/types/draft";
import { elapsedLabel } from "@/lib/elapsed";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { AuditTimeline } from "./AuditTimeline";
import { LocalTime } from "./LocalTime";
import { PipelineStrip } from "./PipelineStrip";
import { SummaryBanner } from "./SummaryBanner";

export const sectionLabel = "text-xs font-medium uppercase tracking-wide text-muted-foreground";

// The selected command as a case file: header, outcome banner, pipeline stepper, timeline.
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
  const elapsed = elapsedLabel(command.status, events);
  return (
    <article className="flex min-w-0 flex-col gap-4" aria-label="Case file">
      <header className="min-w-0">
        <h2 className={sectionLabel}>Case file</h2>
        {/* The full text lives only in this element's title, so no tooltip covers the banner. */}
        <p
          className="mt-2 line-clamp-2 wrap-anywhere text-xl font-semibold leading-snug text-balance"
          title={command.text}
        >
          {command.text}
        </p>
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          <span className="font-mono">{command.id.slice(0, 8)}</span>
          <LocalTime iso={command.createdAt} showDate />
          {elapsed && <span className="tabular-nums">{elapsed}</span>}
        </p>
      </header>
      <SummaryBanner
        text={summarize(command.status, events)}
        tone={bannerTone(command.status, events)}
      />
      <PipelineStrip stages={derivePipeline(events)} />
      <section aria-label="Audit timeline" className="min-w-0 rounded-ui-lg bg-surface p-4">
        <h3 className={`${sectionLabel} mb-4`}>Audit timeline</h3>
        <AuditTimeline events={events} highlightIds={highlightIds} />
      </section>
    </article>
  );
}
