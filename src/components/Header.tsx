import { FlaskConical, ShieldCheck } from "lucide-react";
import { COMMAND_STATUSES, type CommandStatus } from "@/types/draft";
import { ConnectionIndicator, type ConnectionStatus } from "./ConnectionIndicator";

// Literal class names so Tailwind can see them.
const dot: Record<CommandStatus, string> = {
  received: "bg-status-received-accent",
  approved: "bg-status-approved-accent",
  rejected: "bg-status-rejected-accent",
  executing: "bg-status-executing-accent",
  completed: "bg-status-completed-accent",
  failed: "bg-status-failed-accent",
};

export function Brand() {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <span
        aria-hidden="true"
        className="flex size-9 shrink-0 items-center justify-center rounded-ui-md bg-primary/12 text-primary ring-1 ring-primary/40 shadow-[0_0_20px_-2px_rgb(77_163_255/0.45)]"
      >
        <ShieldCheck className="size-4" />
      </span>
      <div className="min-w-0">
        <h1 className="truncate text-base leading-tight font-semibold tracking-tight">Robot Audit Dashboard</h1>
        <p className="hidden truncate text-xs text-muted-foreground sm:block">
          Every robot action is checked twice and logged
        </p>
      </div>
    </div>
  );
}

// Static for now: the worker only dry-runs. One line to change when that is no longer true.
const MODE = "Dry run";

// Sticky top bar. Counts show nonzero statuses only, and nothing at all when there are none.
export function TopBar({
  status,
  counts,
}: {
  status: ConnectionStatus;
  counts: Record<CommandStatus, number>;
}) {
  const shown = COMMAND_STATUSES.filter((s) => counts[s] > 0);
  return (
    <header className="sticky top-0 z-20 border-b border-border bg-background/90 backdrop-blur supports-[backdrop-filter]:bg-background/70">
      <div className="mx-auto flex min-h-14 w-full max-w-7xl flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-2 sm:px-6">
        <Brand />
        <div className="flex min-w-0 flex-wrap items-center justify-end gap-x-4 gap-y-1">
          {shown.length > 0 && (
            <ul
              aria-label="Commands by status"
              className="flex flex-wrap items-center divide-x divide-border overflow-hidden rounded-ui-md border border-border bg-surface text-xs leading-4 text-muted-foreground"
            >
              {shown.map((s) => (
                <li key={s} className="inline-flex items-center gap-2 px-3 py-1">
                  <span aria-hidden="true" className={`size-1.5 rounded-full ${dot[s]}`} />
                  <span className="font-mono font-medium text-foreground tabular-nums">{counts[s]}</span>
                  <span>{s}</span>
                </li>
              ))}
            </ul>
          )}
          <span
            title="Nothing is sent to a robot"
            className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-xs leading-4 text-muted-foreground"
          >
            <FlaskConical aria-hidden="true" className="size-3 text-status-info-accent" />
            <span>Mode</span>
            <span className="font-medium text-foreground">{MODE}</span>
            <span className="sr-only">: nothing is sent to a robot</span>
          </span>
          <ConnectionIndicator status={status} />
        </div>
      </div>
    </header>
  );
}
