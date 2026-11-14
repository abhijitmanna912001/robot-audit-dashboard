export type ConnectionStatus = "live" | "connecting" | "disconnected";

const dot: Record<ConnectionStatus, string> = {
  live: "bg-status-completed-foreground",
  connecting: "bg-status-rejected-foreground",
  disconnected: "bg-destructive",
};

export function ConnectionIndicator({ status }: { status: ConnectionStatus }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground"
      role="status"
    >
      <span className={`size-2 rounded-full ${dot[status]}`} />
      {status}
    </span>
  );
}
