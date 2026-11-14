import { Suspense } from "react";
import { notFound } from "next/navigation";
import { CaseFile, sectionLabel } from "@/components/CaseFile";
import { derivePipeline } from "@/lib/pipeline";
import { ALL } from "./constants";
import { scenarios } from "./fixtures";
import { ScenarioList } from "./ScenarioList";
import { ViewportFrame } from "./ViewportFrame";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

const caseFileFor = (s: (typeof scenarios)[number]) => (
  <CaseFile
    command={{
      id: s.events[0]?.commandId ?? "00000000",
      text: s.title,
      status: s.status,
      createdAt: s.events[0]?.createdAt ?? "1970-01-01T00:00:00Z",
    }}
    events={s.events}
  />
);

// Reads searchParams (runtime data), so it sits inside <Suspense>. ?scenario=<id> picks one
// scenario, ?scenario=all the stacked view; a missing or unknown id falls back to the first.
// The list is the only client piece besides the width toggle; the case files stay server-rendered.
async function PreviewBody({ searchParams }: { searchParams: SearchParams }) {
  const raw = (await searchParams).scenario;
  const wanted = Array.isArray(raw) ? raw[0] : raw;
  const current = wanted === ALL || scenarios.some((s) => s.id === wanted) ? (wanted as string) : scenarios[0].id;
  const rows = scenarios.map((s) => ({
    id: s.id,
    title: s.title,
    status: s.status,
    stages: derivePipeline(s.events),
  }));
  const shown = current === ALL ? scenarios : scenarios.filter((s) => s.id === current);
  const title = current === ALL ? "All scenarios (stacked)" : (shown[0]?.title ?? "");

  return (
    <>
      <aside
        aria-label="Scenario list"
        className="scroll-subtle flex min-h-0 w-[300px] shrink-0 flex-col gap-3 overflow-y-auto border-r border-border p-3 [scrollbar-gutter:stable]"
      >
        <h1 className={sectionLabel}>Case file preview (fixtures)</h1>
        <ScenarioList rows={rows} current={current} />
        <p className="mt-auto pt-3 text-xs leading-4 text-muted-foreground">
          Append-only audit log. Every event is recorded and readable.
        </p>
      </aside>
      {/* Remounted per selection, so the right pane always starts at the top. */}
      <ViewportFrame key={current} title={title}>
        <div aria-labelledby={`scenario-tab-${current}`} className="flex flex-col gap-8">
          {shown.map((s) => (
            <section key={s.id} className="min-w-0">
              {current === ALL && <h2 className={`${sectionLabel} mb-2`}>Scenario: {s.title}</h2>}
              {caseFileFor(s)}
            </section>
          ))}
        </div>
      </ViewportFrame>
    </>
  );
}

function PreviewFallback() {
  return (
    <>
      <div aria-hidden="true" className="skeleton m-3 w-[276px] shrink-0 rounded-ui-lg" />
      <div aria-hidden="true" className="skeleton m-3 min-w-0 flex-1 rounded-ui-lg" />
    </>
  );
}

// Dev-only: renders scenarios inside the real case-file layout from fixtures, so the UI can be
// checked without writing rows to the real database. Not available in production builds.
// The production check lives in this synchronous shell (not in the suspended child) so the
// prerender itself is a 404; notFound() after the shell has streamed could not change the status.
export default function DevPreviewPage({ searchParams }: { searchParams: SearchParams }) {
  if (process.env.NODE_ENV === "production") notFound();

  return (
    <main className="flex h-dvh w-full overflow-hidden">
      <Suspense fallback={<PreviewFallback />}>
        <PreviewBody searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
