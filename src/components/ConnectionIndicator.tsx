export type ConnectionStatus = "live" | "connecting" | "disconnected";

const dot: Record<ConnectionStatus, string> = {
  live: "bg-green-500",
  connecting: "bg-yellow-500",
  disconnected: "bg-red-500",
};

export function ConnectionIndicator({ status }: { status: ConnectionStatus }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-zinc-500" role="status">
      <span className={`h-2 w-2 rounded-full ${dot[status]}`} />
      {status}
    </span>
  );
}
