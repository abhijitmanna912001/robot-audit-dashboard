import type { CommandStatus } from "@/types/draft";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

// Colours come from the --status-* tokens in globals.css (background + foreground pair).
// Executing gets a dot as well as a colour, so it never relies on hue alone to tell
// it apart from Rejected.
const styles: Record<CommandStatus, string> = {
  received: "bg-status-received text-status-received-foreground",
  approved: "bg-status-approved text-status-approved-foreground",
  rejected: "bg-status-rejected text-status-rejected-foreground",
  executing: "bg-status-executing text-status-executing-foreground",
  completed: "bg-status-completed text-status-completed-foreground",
  failed: "bg-status-failed text-status-failed-foreground",
};

export function StatusBadge({ status }: { status: CommandStatus }) {
  return (
    <Badge className={cn("h-auto px-2 py-px text-xs capitalize", styles[status])}>
      {status === "executing" && (
        <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
      )}
      {status}
    </Badge>
  );
}
