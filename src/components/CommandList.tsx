import Link from "next/link";
import type { Command } from "@/types/draft";
import { StatusBadge } from "./StatusBadge";

export function CommandList({
  commands,
  selectedId,
}: {
  commands: Command[];
  selectedId?: string;
}) {
  return (
    <ul className="flex flex-col gap-2">
      {commands.map((c) => (
        <li key={c.id}>
          <Link
            href={`/?command=${c.id}`}
            className={`block rounded-md border p-3 text-sm ${
              c.id === selectedId
                ? "border-blue-500 bg-blue-50 dark:bg-blue-950"
                : "border-zinc-300 hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <span>{c.text}</span>
              <StatusBadge status={c.status} />
            </div>
            <time className="mt-1 block text-xs text-zinc-500" dateTime={c.createdAt}>
              {new Date(c.createdAt).toISOString().replace("T", " ").slice(0, 19)} UTC
            </time>
          </Link>
        </li>
      ))}
    </ul>
  );
}
