import type { CommandStatus } from "@/types/draft";

// Literal class names so Tailwind can see them. One small dot per status, shared by the command
// list and the /dev-preview scenario list.
export const statusDot: Record<CommandStatus, string> = {
  received: "bg-status-received-accent",
  approved: "bg-status-approved-accent",
  rejected: "bg-status-rejected-accent",
  executing: "bg-status-executing-accent",
  completed: "bg-status-completed-accent",
  failed: "bg-status-failed-accent",
};
