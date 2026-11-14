import { notFound } from "next/navigation";
import { CaseFile, sectionLabel } from "@/components/CaseFile";
import { scenarios } from "./fixtures";

// Dev-only: renders every scenario inside the real case-file layout from fixtures, so the UI
// can be checked without writing rows to the real database. Not available in production builds.
export default function DevPreviewPage() {
  if (process.env.NODE_ENV === "production") notFound();

  return (
    <main className="mx-auto w-full max-w-3xl space-y-10 px-4 py-6 sm:px-6">
      <h1 className="text-xl font-semibold">Case file preview (fixtures)</h1>
      {scenarios.map((s) => (
        <section key={s.title} className="min-w-0">
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
    </main>
  );
}
