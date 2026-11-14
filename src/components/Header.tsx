import { ShieldCheck } from "lucide-react";
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
        className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary-foreground ring-1 ring-primary/40"
      >
        <ShieldCheck className="size-4" />
      </span>
      <div className="min-w-0">
        <h1 className="truncate text-base font-semibold leading-tight">Robot Audit Dashboard</h1>
        <p className="hidden truncate text-xs text-muted-foreground sm:block">
          Every robot action is checked twice and logged
        </p>
      </div>
    </div>
  );
}

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
    <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="mx-auto flex min-h-14 w-full max-w-7xl flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-2 sm:px-6">
        <Brand />
        <div className="flex min-w-0 flex-wrap items-center justify-end gap-x-4 gap-y-1">
          {shown.length > 0 && (
            <ul aria-label="Commands by status" className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
              {shown.map((s) => (
                <li key={s} className="inline-flex items-center gap-1.5">
                  <span aria-hidden="true" className={`size-2 rounded-full ${dot[s]}`} />
                  <span className="tabular-nums font-medium text-foreground">{counts[s]}</span>
                  <span>{s}</span>
                </li>
              ))}
            </ul>
          )}
          <ConnectionIndicator status={status} />
        </div>
      </div>
    </header>
  );
}
