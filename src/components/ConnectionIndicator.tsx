export type ConnectionStatus = "live" | "connecting" | "disconnected";

const dot: Record<ConnectionStatus, string> = {
  live: "bg-status-completed-accent",
  connecting: "bg-status-failed-accent",
  disconnected: "bg-status-rejected-accent",
};

export function ConnectionIndicator({ status }: { status: ConnectionStatus }) {
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-xs leading-4 text-muted-foreground"
      role="status"
    >
      <span
        aria-hidden="true"
        className={`size-2 rounded-full ${dot[status]} ${status === "live" ? "live-dot" : ""}`}
      />
      {status}
    </span>
  );
}
