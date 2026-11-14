import type { Command } from "@/types/draft";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { StatusBadge } from "./StatusBadge";

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
    return <p className="text-sm text-muted-foreground">No commands yet.</p>;
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
                "block w-full min-w-0 rounded-xl border bg-card p-3 text-left text-sm text-card-foreground outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                selected
                  ? "border-primary bg-accent ring-1 ring-primary"
                  : "border-border hover:bg-accent/50",
              )}
            >
              <div className="flex min-w-0 items-start justify-between gap-2">
                <span className="line-clamp-2 min-w-0 flex-1 wrap-anywhere" title={c.text}>
                  {c.text}
                </span>
                <StatusBadge status={c.status} />
              </div>
              <time className="mt-1.5 block text-xs text-muted-foreground" dateTime={c.createdAt}>
                {formatDateTime(c.createdAt)}
              </time>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
