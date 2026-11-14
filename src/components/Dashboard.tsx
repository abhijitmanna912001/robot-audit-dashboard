"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getBrowserClient } from "@/lib/supabase/browser";
import { fetchSnapshot } from "@/lib/queries";
import { toAuditEvent, toCommand } from "@/lib/mappers";
import { mergeCommands, mergeEvents } from "@/lib/merge";
import { summarize } from "@/lib/summary";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { AuditEvent, Command } from "@/types/draft";
import { AuditTimeline } from "./AuditTimeline";
import { CommandInput } from "./CommandInput";
import { CommandList } from "./CommandList";
import { ConnectionIndicator, type ConnectionStatus } from "./ConnectionIndicator";
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

  // Selection only auto-fills while empty; it never moves on its own after that.
  const applyCommands = useCallback((incoming: Command[]) => {
    setCommands((prev) => mergeCommands(prev, incoming));
    setSelectedId((prev) => prev ?? incoming[0]?.id ?? null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!client) return;
    const supabase = client;

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
      if (e) setEvents((prev) => mergeEvents(prev, [e]));
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

  return (
    <>
      <CommandInput onSubmitted={handleSubmitted} />
      <div className="flex flex-wrap items-center gap-3">
        <ConnectionIndicator status={status} />
        {loadError && (
          <span className="text-xs text-destructive">Could not load or refresh data</span>
        )}
      </div>
      <div className="grid gap-6 md:grid-cols-2">
        <section className="min-w-0">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-sm font-medium text-muted-foreground">Commands</h2>
            <Button
              type="button"
              variant="outline"
              size="xs"
              aria-pressed={showTest}
              onClick={() => setShowTest((v) => !v)}
            >
              {showTest ? "Hide test commands" : "Show test commands"}
            </Button>
          </div>
          {hiddenTestCount > 0 && (
            <p className="mb-2 text-xs text-muted-foreground" role="status">
              {hiddenTestCount} test {hiddenTestCount === 1 ? "command" : "commands"} hidden
            </p>
          )}
          <CommandList
            commands={visibleCommands}
            selectedId={selectedId ?? undefined}
            onSelect={select}
          />
        </section>
        <section className="min-w-0">
          <h2
            className="mb-3 line-clamp-2 wrap-anywhere text-sm font-medium text-muted-foreground"
            title={selected?.text}
          >
            Audit timeline{selected ? `: ${selected.text}` : ""}
          </h2>
          {selected && (
            <p className="wrap-anywhere mb-3 rounded-lg border border-border bg-muted/40 px-3 py-2 text-sm font-medium">
              {summarize(selected.status, selectedEvents)}
            </p>
          )}
          <Card size="sm">
            <CardContent>
              <AuditTimeline events={selectedEvents} />
            </CardContent>
          </Card>
        </section>
      </div>
    </>
  );
}
