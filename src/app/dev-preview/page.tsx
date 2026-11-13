import { notFound } from "next/navigation";
import { AuditTimeline } from "@/components/AuditTimeline";
import { summarize } from "@/lib/summary";
import { scenarios } from "./fixtures";

// Dev-only: renders the timeline from fixtures so the UI can be checked without
// writing rows to the real database. Not available in production builds.
export default function DevPreviewPage() {
  if (process.env.NODE_ENV === "production") notFound();

  return (
    <main className="mx-auto max-w-3xl space-y-10 p-6">
      <h1 className="text-lg font-semibold">Audit timeline preview (fixtures)</h1>
      {scenarios.map((s) => (
        <section key={s.title} className="min-w-0">
          <h2 className="mb-1 text-sm font-medium text-zinc-500">{s.title}</h2>
          <p className="wrap-anywhere mb-3 text-sm font-medium">{summarize(s.status, s.events)}</p>
          <AuditTimeline events={s.events} />
        </section>
      ))}
    </main>
  );
}
