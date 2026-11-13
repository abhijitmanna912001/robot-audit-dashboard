import type { CommandStatus } from "@/types/draft";

const styles: Record<CommandStatus, string> = {
  received: "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100",
  approved: "bg-blue-200 text-blue-900 dark:bg-blue-900 dark:text-blue-100",
  rejected: "bg-orange-200 text-orange-900 dark:bg-orange-900 dark:text-orange-100",
  executing: "bg-cyan-200 text-cyan-900 dark:bg-cyan-800 dark:text-cyan-100",
  completed: "bg-green-200 text-green-900 dark:bg-green-900 dark:text-green-100",
  failed: "bg-red-600 text-white dark:bg-red-700",
};

export function StatusBadge({ status }: { status: CommandStatus }) {
  return (
    <span
      className={`inline-block shrink-0 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${styles[status]}`}
    >
      {status}
    </span>
  );
}
