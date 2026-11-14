import { Suspense } from "react";
import { Dashboard } from "@/components/Dashboard";
import { Brand } from "@/components/Header";
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

function Skeleton({ className }: { className: string }) {
  return <div aria-hidden="true" className={`skeleton rounded-ui-md ${className}`} />;
}

// Loading state only: same shell as the dashboard, so the page does not jump.
function LoadingShell() {
  return (
    <div role="status" aria-label="Loading" className="flex min-h-dvh flex-col">
      <div className="sticky top-0 z-20 border-b border-border bg-background/90">
        <div className="mx-auto flex min-h-14 w-full max-w-[1440px] items-center px-4 py-6 sm:px-8">
          <Brand />
        </div>
      </div>
      <div className="mx-auto grid w-full max-w-[1440px] flex-1 gap-8 px-4 py-8 sm:px-8 lg:grid-cols-[336px_minmax(0,1fr)]">
        <div className="flex flex-col gap-3">
          <Skeleton className="h-64" />
          <Skeleton className="h-8" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
        <div className="flex flex-col gap-4">
          <Skeleton className="h-16" />
          <Skeleton className="h-10" />
          <Skeleton className="h-20" />
          <Skeleton className="h-64" />
        </div>
      </div>
    </div>
  );
}

export default function Home({ searchParams }: { searchParams: SearchParams }) {
  return (
    <main className="w-full">
      <Suspense fallback={<LoadingShell />}>
        <LoadedDashboard searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
