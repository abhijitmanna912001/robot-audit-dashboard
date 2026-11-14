// Display-only filtering and counting for the command list. Pure.
// The list and the top-bar counts both use these, so the numbers match the rows.
import { COMMAND_STATUSES, type Command, type CommandStatus } from "../types/draft.ts";
import { isTestCommand } from "../components/test-commands.ts";

export type Tab = "all" | "attention" | "completed";

const TAB_STATUSES: Record<Exclude<Tab, "all">, readonly CommandStatus[]> = {
  attention: ["rejected", "failed"],
  completed: ["completed"],
};

// Test commands are hidden unless `showTest`; the tab narrows by status. The selected
// command is never hidden by either, so the open case file always keeps its row.
export function filterCommands<T extends Pick<Command, "id" | "text" | "status">>(
  commands: readonly T[],
  opts: { tab: Tab; showTest: boolean; selectedId?: string | null },
): T[] {
  return commands.filter((c) => {
    if (c.id === opts.selectedId) return true;
    if (!opts.showTest && isTestCommand(c.text)) return false;
    return opts.tab === "all" || TAB_STATUSES[opts.tab].includes(c.status);
  });
}

export function statusCounts(commands: readonly Pick<Command, "status">[]): Record<CommandStatus, number> {
  const counts = Object.fromEntries(COMMAND_STATUSES.map((s) => [s, 0])) as Record<CommandStatus, number>;
  for (const c of commands) if (Object.hasOwn(counts, c.status)) counts[c.status] += 1;
  return counts;
}
