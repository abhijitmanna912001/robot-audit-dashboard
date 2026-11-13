import { Suspense } from "react";
import { Dashboard } from "@/components/Dashboard";
import { fetchSnapshot } from "@/lib/queries";
import { createAnonServerClient } from "@/lib/supabase/anon";

type SearchParams = Promise<{ command?: string }>;

// Reads searchParams and the database, so it must sit inside <Suspense>.
async function LoadedDashboard({ searchParams }: { searchParams: SearchParams }) {
  const { command } = await searchParams;

  let commands: Awaited<ReturnType<typeof fetchSnapshot>> = { ok: false };
  try {
    // Anon key only: reads are limited by RLS. The service-role client is not used here.
    commands = await fetchSnapshot(createAnonServerClient());
  } catch {
    // Missing env etc. Render empty; the client refetches once subscribed.
  }

  const data = commands.ok ? commands.data : { commands: [], events: [] };
  const selectedId =
    data.commands.find((c) => c.id === command)?.id ?? data.commands[0]?.id ?? null;

  return (
    <Dashboard
      initialCommands={data.commands}
      initialEvents={data.events}
      initialSelectedId={selectedId}
      initialError={!commands.ok}
    />
  );
}

export default function Home({ searchParams }: { searchParams: SearchParams }) {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
      <h1 className="text-xl font-semibold">Robot Audit Dashboard</h1>
      <Suspense fallback={<p className="text-sm text-zinc-500">Loading...</p>}>
        <LoadedDashboard searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
