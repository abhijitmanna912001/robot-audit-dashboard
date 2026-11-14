import { notFound } from "next/navigation";
import { CaseFile, sectionLabel } from "@/components/CaseFile";
import { ALL, scenarios } from "./fixtures";
import { ScenarioPicker } from "./ScenarioPicker";

// Dev-only: renders scenarios inside the real case-file layout from fixtures, so the UI
// can be checked without writing rows to the real database. Not available in production builds.
// ?scenario=<id> picks one scenario, ?scenario=all shows every one; a missing or unknown id
// falls back to the first scenario. Server component: only the picker is a client component.
export default async function DevPreviewPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  if (process.env.NODE_ENV === "production") notFound();

  const raw = (await searchParams).scenario;
  const wanted = Array.isArray(raw) ? raw[0] : raw;
  const current = wanted === ALL || scenarios.some((s) => s.id === wanted) ? (wanted as string) : scenarios[0].id;
  const shown = current === ALL ? scenarios : scenarios.filter((s) => s.id === current);

  return (
    <main className="mx-auto w-full max-w-3xl space-y-6 px-4 py-6 sm:px-6">
      <h1 className="text-xl font-semibold">Case file preview (fixtures)</h1>
      <ScenarioPicker scenarios={scenarios.map(({ id, title }) => ({ id, title }))} current={current} />
      <div role="tabpanel" id="scenario-panel" aria-labelledby={`scenario-tab-${current}`} className="space-y-10">
        {shown.map((s) => (
          <section key={s.id} className="min-w-0">
            <h2 className={`${sectionLabel} mb-2`}>Scenario: {s.title}</h2>
            <CaseFile
              command={{
                id: s.events[0]?.commandId ?? "00000000",
                text: s.title,
                status: s.status,
                createdAt: s.events[0]?.createdAt ?? "1970-01-01T00:00:00Z",
              }}
              events={s.events}
            />
          </section>
        ))}
      </div>
    </main>
  );
}
