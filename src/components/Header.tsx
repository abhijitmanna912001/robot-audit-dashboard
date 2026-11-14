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
        className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary ring-1 ring-primary/20"
      >
        <ShieldCheck className="size-5" />
      </span>
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">Robot Audit Dashboard</h1>
        <p className="text-sm text-muted-foreground">Every robot action is checked twice and logged</p>
      </div>
    </div>
  );
}

export function DashboardHeader({
  status,
  counts,
}: {
  status: ConnectionStatus;
  counts: Record<CommandStatus, number>;
}) {
  return (
    <header className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <Brand />
        <ConnectionIndicator status={status} />
      </div>
      <ul aria-label="Commands by status" className="flex flex-wrap gap-2">
        {COMMAND_STATUSES.map((s) => (
          <li
            key={s}
            className={`inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-xs ${
              counts[s] === 0 ? "text-muted-foreground" : "text-card-foreground"
            }`}
          >
            <span aria-hidden="true" className={`size-2 rounded-full ${dot[s]} ${counts[s] === 0 ? "opacity-40" : ""}`} />
            <span className="capitalize">{s}</span>
            <span className="font-semibold tabular-nums">{counts[s]}</span>
          </li>
        ))}
      </ul>
    </header>
  );
}
