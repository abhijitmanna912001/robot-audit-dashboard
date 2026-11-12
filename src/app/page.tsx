import { Suspense } from "react";
import { AuditTimeline } from "@/components/AuditTimeline";
import { CommandInput } from "@/components/CommandInput";
import { CommandList } from "@/components/CommandList";
import { mockAuditEvents, mockCommands } from "@/mocks/mock-data";

type SearchParams = Promise<{ command?: string }>;

// Reads searchParams, so it must sit inside <Suspense> for prerendering.
async function CommandsAndTimeline({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const { command } = await searchParams;
  const selected =
    mockCommands.find((c) => c.id === command) ?? mockCommands[0];
  const events = mockAuditEvents.filter((e) => e.commandId === selected?.id);

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <section>
        <h2 className="mb-3 text-sm font-medium text-zinc-500">Commands</h2>
        <CommandList commands={mockCommands} selectedId={selected?.id} />
      </section>
      <section>
        <h2 className="mb-3 text-sm font-medium text-zinc-500">
          Audit timeline{selected ? `: ${selected.text}` : ""}
        </h2>
        <AuditTimeline events={events} />
      </section>
    </div>
  );
}

export default function Home({ searchParams }: { searchParams: SearchParams }) {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
      <h1 className="text-xl font-semibold">Robot Audit Dashboard</h1>

      <CommandInput />

      <Suspense fallback={<p className="text-sm text-zinc-500">Loading...</p>}>
        <CommandsAndTimeline searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
