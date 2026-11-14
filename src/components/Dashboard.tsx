"use client";

import { FileSearch } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getBrowserClient } from "@/lib/supabase/browser";
import { fetchSnapshot } from "@/lib/queries";
import { toAuditEvent, toCommand } from "@/lib/mappers";
import { mergeCommands, mergeEvents } from "@/lib/merge";
import { filterCommands, statusCounts, type Tab } from "@/lib/command-filter";
import { derivePipeline } from "@/lib/pipeline";
import type { AuditEvent, Command } from "@/types/draft";
import { CaseFile, sectionLabel } from "./CaseFile";
import { CommandInput } from "./CommandInput";
import { CommandList } from "./CommandList";
import type { ConnectionStatus } from "./ConnectionIndicator";
import { FilterTabs } from "./FilterTabs";
import { HowItWorks } from "./HowItWorks";
import { TopBar } from "./Header";
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
  const [tab, setTab] = useState<Tab>("all"); // display filter only
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

  // Display only: the four-dot mini pipeline in each row, derived from the events already held.
  const stagesById = useMemo(() => {
    const byCommand = new Map<string, AuditEvent[]>();
    for (const e of events) {
      const list = byCommand.get(e.commandId);
      if (list) list.push(e);
      else byCommand.set(e.commandId, [e]);
    }
    return new Map(commands.map((c) => [c.id, derivePipeline(byCommand.get(c.id) ?? [])]));
  }, [commands, events]);

  const selected = commands.find((c) => c.id === selectedId);
  const selectedEvents = events.filter((e) => e.commandId === selectedId);

  // Display only: the commands state and Realtime merge are untouched. The selected
  // command is never hidden, so the open case file always keeps its row.
  const hiddenTestCount = showTest
    ? 0
    : commands.filter((c) => isTestCommand(c.text) && c.id !== selectedId).length;
  const visibleCommands = filterCommands(commands, { tab, showTest, selectedId });
  // Top-bar counts follow the test-command toggle but not the tab, so picking a tab does not
  // blank the other statuses.
  const counts = statusCounts(filterCommands(commands, { tab: "all", showTest, selectedId }));

  const emptyText =
    commands.length === 0
      ? "No commands yet. Type one above and it will appear here."
      : tab === "attention"
        ? "Nothing needs attention."
        : tab === "completed"
          ? "No completed commands yet."
          : "No commands to show.";

  return (
    <div className="flex min-h-dvh flex-col lg:h-dvh lg:overflow-hidden">
      <TopBar status={status} counts={counts} />
      {loadError && (
        <div className="mx-auto w-full max-w-[1440px] shrink-0 px-4 pt-4 sm:px-8">
          <p role="alert" className="rounded-ui-md border border-destructive bg-status-rejected px-4 py-3 text-sm text-destructive">
            Could not load or refresh data
          </p>
        </div>
      )}
      <div className="mx-auto flex min-h-0 w-full max-w-[1440px] flex-1 flex-col gap-6 px-4 py-6 sm:px-8 lg:grid lg:grid-cols-[336px_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)] lg:gap-8 lg:py-8">
        <section aria-label="Commands" className="scroll-subtle flex min-w-0 flex-col gap-5 lg:min-h-0 lg:overflow-y-auto lg:p-1.5 lg:pr-4 lg:[scrollbar-gutter:stable]">
          <CommandInput onSubmitted={handleSubmitted} />
          {selected && (
            <a href="#audit-record" className="self-start rounded-ui-sm py-1 text-sm font-medium text-primary underline underline-offset-4 lg:hidden">
              View selected audit <span aria-hidden="true">↓</span>
            </a>
          )}
          <FilterTabs
            tab={tab}
            onTab={setTab}
            showTest={showTest}
            onShowTest={() => setShowTest((v) => !v)}
            hiddenTestCount={hiddenTestCount}
          />
          <h2 className={sectionLabel}>Command history</h2>
          <CommandList
            commands={visibleCommands}
            selectedId={selectedId ?? undefined}
            onSelect={select}
            stagesById={stagesById}
            emptyText={emptyText}
          />
          <HowItWorks />
        </section>
        {/* Keyed by the selection: switching commands remounts the scroll container, so it starts
            at the top (no scroll code). The padding keeps focus rings and shadows from being
            clipped; the stable gutter keeps the scrollbar off the cards. */}
        <section
          id="audit-record"
          aria-label="Selected command audit"
          tabIndex={-1}
          key={selectedId ?? "none"}
          className="scroll-subtle min-w-0 scroll-pt-6 lg:min-h-0 lg:overflow-y-auto lg:p-1.5 lg:pr-3 lg:[scrollbar-gutter:stable]"
        >
          {selected ? (
            <CaseFile command={selected} events={selectedEvents} highlightIds={freshIds} />
          ) : (
            <div className="surface-card flex min-h-80 flex-col items-start justify-center gap-4 rounded-ui-lg px-6 py-12 sm:px-10">
              <FileSearch aria-hidden="true" className="size-8 text-primary" />
              <p className="text-2xl font-semibold tracking-tight">Every command has a record.</p>
              <p className="max-w-sm text-sm leading-6 text-muted-foreground">Select a command to inspect its plan, independent checks, and audit events.</p>
              <span className="font-mono text-xs text-muted-foreground">DRY RUN ONLY · NOTHING SENT TO A ROBOT</span>
            </div>
          )}
        </section>
      </div>
      <footer className="shrink-0 border-t border-border px-4 py-3 text-center text-xs text-muted-foreground sm:px-6">
        Append-only audit log. Every event is recorded and readable.
      </footer>
    </div>
  );
}
