import type { Command } from "@/types/draft";
import { formatDateTime } from "@/lib/format";
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
    return <p className="text-sm text-zinc-500">No commands yet.</p>;
  }
  return (
    <ul className="flex flex-col gap-2">
      {commands.map((c) => (
        <li key={c.id} className="min-w-0">
          <button
            type="button"
            onClick={() => onSelect(c.id)}
            className={`block w-full min-w-0 rounded-md border p-3 text-left text-sm ${
              c.id === selectedId
                ? "border-blue-500 bg-blue-50 dark:bg-blue-950"
                : "border-zinc-300 hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
            }`}
          >
            <div className="flex min-w-0 items-start justify-between gap-2">
              <span className="line-clamp-2 min-w-0 flex-1 wrap-anywhere" title={c.text}>
                {c.text}
              </span>
              <StatusBadge status={c.status} />
            </div>
            <time className="mt-1 block text-xs text-zinc-500" dateTime={c.createdAt}>
              {formatDateTime(c.createdAt)}
            </time>
          </button>
        </li>
      ))}
    </ul>
  );
}
