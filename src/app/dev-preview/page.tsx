import { notFound } from "next/navigation";
import { AuditTimeline } from "@/components/AuditTimeline";
import { PipelineStrip } from "@/components/PipelineStrip";
import { SummaryBanner } from "@/components/SummaryBanner";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { scenarios } from "./fixtures";

// Dev-only: renders the timeline from fixtures so the UI can be checked without
// writing rows to the real database. Not available in production builds.
export default function DevPreviewPage() {
  if (process.env.NODE_ENV === "production") notFound();

  return (
    <main className="mx-auto max-w-3xl space-y-10 p-4 sm:p-6">
      <h1 className="text-lg font-semibold">Audit timeline preview (fixtures)</h1>
      {scenarios.map((s) => (
        <section key={s.title} className="flex min-w-0 flex-col gap-3">
          <h2 className="text-sm font-medium text-muted-foreground">{s.title}</h2>
          <SummaryBanner text={summarize(s.status, s.events)} tone={bannerTone(s.status, s.events)} />
          <PipelineStrip stages={derivePipeline(s.events)} />
          <AuditTimeline events={s.events} />
        </section>
      ))}
    </main>
  );
}
