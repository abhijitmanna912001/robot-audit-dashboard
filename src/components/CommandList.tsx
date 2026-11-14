import { Inbox } from "lucide-react";
import type { Command, CommandStatus } from "@/types/draft";
import { cn } from "@/lib/utils";
import { LocalTime } from "./LocalTime";
import { StatusBadge } from "./StatusBadge";

// Literal class names so Tailwind can see them. The left border carries the status colour.
const leftBorder: Record<CommandStatus, string> = {
  received: "border-l-status-received-accent",
  approved: "border-l-status-approved-accent",
  rejected: "border-l-status-rejected-accent",
  executing: "border-l-status-executing-accent",
  completed: "border-l-status-completed-accent",
  failed: "border-l-status-failed-accent",
};

// First 8 characters of the id, as plain text (ids are uuids; the row also has a title).
const shortId = (id: string) => id.slice(0, 8);

export function CommandList({
  commands,
  selectedId,
  onSelect,
}: {
  commands: Command[];
  selectedId?: string;
  onSelect: (id: string) => void;
}) {
  if (commands.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border px-4 py-10 text-center">
        <Inbox aria-hidden="true" className="size-8 text-muted-foreground" />
        <p className="text-sm font-medium">No commands yet</p>
        <p className="text-xs text-muted-foreground">
          Type a command above. It will appear here and move through the pipeline.
        </p>
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-2">
      {commands.map((c) => {
        const selected = c.id === selectedId;
        return (
          <li key={c.id} className="min-w-0">
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={selected ? "true" : undefined}
              className={cn(
                "block w-full min-w-0 rounded-xl border border-l-4 p-3 text-left text-sm text-card-foreground outline-none transition-colors duration-150 focus-visible:ring-3 focus-visible:ring-ring/50",
                leftBorder[c.status],
                selected
                  ? "border-y-primary border-r-primary bg-primary/10 ring-2 ring-primary/60"
                  : "border-y-border border-r-border bg-card hover:bg-accent/50",
              )}
            >
              <div className="flex min-w-0 items-start justify-between gap-2">
                <span className="line-clamp-2 min-w-0 flex-1 wrap-anywhere" title={c.text}>
                  {c.text}
                </span>
                <StatusBadge status={c.status} />
              </div>
              <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                <span className="font-mono">{shortId(c.id)}</span>
                <LocalTime iso={c.createdAt} />
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
