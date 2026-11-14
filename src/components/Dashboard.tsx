"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getBrowserClient } from "@/lib/supabase/browser";
import { fetchSnapshot } from "@/lib/queries";
import { toAuditEvent, toCommand } from "@/lib/mappers";
import { mergeCommands, mergeEvents } from "@/lib/merge";
import { bannerTone, derivePipeline } from "@/lib/pipeline";
import { summarize } from "@/lib/summary";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { COMMAND_STATUSES, type AuditEvent, type Command, type CommandStatus } from "@/types/draft";
import { AuditTimeline } from "./AuditTimeline";
import { CommandInput } from "./CommandInput";
import { CommandList } from "./CommandList";
import type { ConnectionStatus } from "./ConnectionIndicator";
import { DashboardHeader } from "./Header";
import { PipelineStrip } from "./PipelineStrip";
import { SummaryBanner } from "./SummaryBanner";
import { LocalTime } from "./LocalTime";
import { isTestCommand } from "./test-commands";

export function Dashboard({
  initialCommands,
  initialEvents,
  initialSelectedId,
  initialError,
}: {
  initialCommands: Command[];
  initialEvents: AuditEvent[];
  initialSelectedId: string | null;
  initialError: boolean;
}) {
  const [commands, setCommands] = useState(initialCommands);
  const [events, setEvents] = useState(initialEvents);
  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId);
  // Null when the public env vars are missing: show "disconnected", never crash.
  const [client] = useState(() => {
    try {
      return getBrowserClient();
    } catch {
      return null;
    }
  });
  const [status, setStatus] = useState<ConnectionStatus>(
    client ? "connecting" : "disconnected",
  );
  const [loadError, setLoadError] = useState(initialError);
  const [showTest, setShowTest] = useState(false); // display filter only, off by default
  const refetchRef = useRef<() => Promise<void>>(async () => {});
  // Ids of events that just arrived over Realtime, highlighted for 1.5s. Display only.
  const [freshIds, setFreshIds] = useState<ReadonlySet<string>>(new Set());
  const flashTimers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  // Selection only auto-fills while empty; it never moves on its own after that.
  const applyCommands = useCallback((incoming: Command[]) => {
    setCommands((prev) => mergeCommands(prev, incoming));
    setSelectedId((prev) => prev ?? incoming[0]?.id ?? null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!client) return;
    const supabase = client;
    const timers = flashTimers.current;

    async function refetch() {
      const result = await fetchSnapshot(supabase);
      if (cancelled) return;
      if (!result.ok) {
        setLoadError(true);
        return;
      }
      setLoadError(false);
      applyCommands(result.data.commands);
      setEvents((prev) => mergeEvents(prev, result.data.events));
    }
    refetchRef.current = refetch;

    const onCommand = (payload: { new: unknown }) => {
      if (cancelled) return;
      const c = toCommand(payload.new);
      if (c) applyCommands([c]);
    };
    const onEvent = (payload: { new: unknown }) => {
      if (cancelled) return;
      const e = toAuditEvent(payload.new);
      if (!e) return;
      setEvents((prev) => mergeEvents(prev, [e]));
      const id = e.id;
      setFreshIds((prev) => new Set(prev).add(id));
      const timer = setTimeout(() => {
        flashTimers.current.delete(timer);
        setFreshIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }, 1500);
      flashTimers.current.add(timer);
    };

    // Unique topic per mount so React strict mode's mount/unmount/mount is safe.
    const channel = supabase
      .channel(`dashboard-${crypto.randomUUID()}`)
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "commands" }, onCommand)
      .on("postgres_changes", { event: "UPDATE", schema: "public", table: "commands" }, onCommand)
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "audit_events" }, onEvent)
      .subscribe((s) => {
        if (cancelled) return;
        if (s === "SUBSCRIBED") {
          setStatus("live");
          void refetch(); // close the gap between the server fetch and the subscription
        } else if (s === "CHANNEL_ERROR" || s === "TIMED_OUT" || s === "CLOSED") {
          setStatus("disconnected");
        }
      });

    return () => {
      cancelled = true;
      refetchRef.current = async () => {};
      for (const t of timers) clearTimeout(t);
      timers.clear();
      void supabase.removeChannel(channel);
    };
  }, [client, applyCommands]);

  function select(id: string) {
    setSelectedId(id);
    try {
      window.history.replaceState(null, "", `/?command=${encodeURIComponent(id)}`);
    } catch {
      // URL sync is a convenience only.
    }
  }

  function handleSubmitted(commandId?: string) {
    if (commandId) select(commandId);
    void refetchRef.current();
  }

  const selected = commands.find((c) => c.id === selectedId);
  const selectedEvents = events.filter((e) => e.commandId === selectedId);

  // Display only: the commands state and Realtime merge are untouched. The selected
  // command is never hidden, so the open timeline always keeps its row.
  const hiddenTestCount = showTest
    ? 0
    : commands.filter((c) => isTestCommand(c.text) && c.id !== selectedId).length;
  const visibleCommands = showTest
    ? commands
    : commands.filter((c) => !isTestCommand(c.text) || c.id === selectedId);

  // Counts follow the list: same commands as shown (test commands excluded while the toggle
  // is off, the selected one always included), so the numbers match the rows.
  const counts = Object.fromEntries(COMMAND_STATUSES.map((st) => [st, 0])) as Record<
    CommandStatus,
    number
  >;
  for (const c of visibleCommands) if (c.status in counts) counts[c.status] += 1;

  const heading = "text-xs font-medium uppercase tracking-wide text-muted-foreground";
  return (
    <>
      <DashboardHeader status={status} counts={counts} />
      {loadError && (
        <p role="alert" className="text-xs text-destructive">
          Could not load or refresh data
        </p>
      )}
      <CommandInput onSubmitted={handleSubmitted} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <section className="min-w-0">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className={heading}>Commands</h2>
            <Button
              type="button"
              variant="outline"
              size="xs"
              aria-pressed={showTest}
              onClick={() => setShowTest((v) => !v)}
              className="transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              {showTest ? "Hide test commands" : "Show test commands"}
            </Button>
          </div>
          {hiddenTestCount > 0 && (
            <p className="mb-2 text-xs tabular-nums text-muted-foreground" role="status">
              {hiddenTestCount} test {hiddenTestCount === 1 ? "command" : "commands"} hidden
            </p>
          )}
          <CommandList
            commands={visibleCommands}
            selectedId={selectedId ?? undefined}
            onSelect={select}
          />
        </section>
        <section className="flex min-w-0 flex-col gap-3 lg:sticky lg:top-4 lg:-mx-1 lg:max-h-[calc(100dvh-2rem)] lg:self-start lg:overflow-y-auto lg:px-1">
          <h2 className={heading}>Audit timeline</h2>
          {selected && (
            <>
              <div className="min-w-0">
                <p
                  className="line-clamp-2 wrap-anywhere text-base font-semibold leading-snug"
                  title={selected.text}
                >
                  {selected.text}
                </p>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  <span className="font-mono">{selected.id.slice(0, 8)}</span>
                  <LocalTime iso={selected.createdAt} />
                </div>
              </div>
              <SummaryBanner
                text={summarize(selected.status, selectedEvents)}
                tone={bannerTone(selected.status, selectedEvents)}
              />
              <PipelineStrip stages={derivePipeline(selectedEvents)} />
            </>
          )}
          <Card size="sm">
            <CardContent>
              <AuditTimeline events={selectedEvents} highlightIds={freshIds} />
            </CardContent>
          </Card>
        </section>
      </div>
    </>
  );
}
