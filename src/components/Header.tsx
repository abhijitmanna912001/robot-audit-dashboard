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
        className="flex size-10 shrink-0 items-center justify-center rounded-ui-sm bg-primary text-primary-foreground"
      >
        <ShieldCheck className="size-5" />
      </span>
      <div className="min-w-0">
        <p className="mb-1 font-mono text-xs font-medium tracking-[0.18em] text-muted-foreground uppercase">Control ledger</p>
        <h1 className="text-[17px] leading-tight font-semibold tracking-tight sm:text-lg">Robot Audit Dashboard</h1>
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
    <header className="sticky top-0 z-20 shrink-0 border-b border-border bg-background/95 backdrop-blur">
      <div className="mx-auto flex min-h-22 w-full max-w-[1440px] flex-wrap items-center justify-between gap-x-6 gap-y-4 px-4 py-4 sm:px-8">
        <Brand />
        <div className="flex min-w-0 flex-wrap items-center gap-x-5 gap-y-2">
          <div className="border-l-2 border-primary pl-3">
            <span className="flex items-center gap-2 text-xs font-semibold text-foreground">
              <FlaskConical aria-hidden="true" className="size-3.5 text-primary" />
              {MODE} environment
            </span>
            <p className="mt-0.5 text-xs text-muted-foreground">Nothing is sent to a robot.</p>
          </div>
          <ConnectionIndicator status={status} />
        </div>
      </div>
      {shown.length > 0 && (
        <div className="border-t border-border">
          <ul
            aria-label="Commands by status"
            className="mx-auto flex w-full max-w-[1440px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2 text-xs leading-4 text-muted-foreground sm:px-8"
          >
            {shown.map((s) => (
              <li key={s} className="inline-flex items-center gap-2 py-0.5">
                <span aria-hidden="true" className={`size-1.5 rounded-full ${dot[s]}`} />
                <span className="font-mono font-medium text-foreground tabular-nums">{counts[s]}</span>
                <span>{s}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </header>
  );
}
