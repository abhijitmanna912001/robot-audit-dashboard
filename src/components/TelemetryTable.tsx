import type { Telemetry } from "@/lib/simulation";
import { cn } from "@/lib/utils";
import { LocalTime } from "./LocalTime";

const num = "px-3 py-2 text-right font-mono tabular-nums";

// Joint readings of a simulated run, in radians. Server-safe (LocalTime is a client component, which
// a server component may render). Only the snapshots that exist get a column. A joint with no reading
// shows "n/a", never 0. "moved" is written out, so colour is never the only cue. The readAt of each
// snapshot is shown as local time, with the UTC time in the title attribute (see LocalTime).
export function TelemetryTable({ telemetry }: { telemetry: Telemetry }) {
  const { columns, showBack, rows } = telemetry;
  return (
    <div
      role="region"
      aria-label="Joint telemetry, scrolls sideways on narrow screens"
      tabIndex={0}
      className="max-w-full overflow-x-auto rounded-ui-sm border border-border outline-none focus-visible:ring-3 focus-visible:ring-ring/30"
    >
      <table className="w-full min-w-max border-collapse text-sm leading-5">
        <caption className="sr-only">Joint telemetry for the simulated move, in radians</caption>
        <thead>
          <tr className="border-b border-border bg-muted text-xs text-muted-foreground">
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Joint
            </th>
            {columns.map((c) => (
              <th key={c.key} scope="col" className="px-3 py-2 text-right font-medium">
                <span className="block text-foreground">{c.label}</span>
                <LocalTime iso={c.readAt} className="block font-mono font-normal" />
              </th>
            ))}
            {showBack && (
              <th scope="col" className="px-3 py-2 text-right font-medium">
                <span className="block text-foreground">Back to start</span>
                <span className="block font-mono font-normal">restored − start</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.joint} className="border-b border-border last:border-b-0">
              <th scope="row" className="px-3 py-2 text-left font-mono font-medium">
                {r.joint}
                {r.moved && (
                  <span className="ml-2 rounded-ui-sm bg-status-info px-1.5 py-0.5 font-sans text-xs font-medium text-status-info-foreground">
                    moved
                  </span>
                )}
              </th>
              {columns.map((c) => (
                <td key={c.key} className={cn(num, r.values[c.key] === undefined && "text-muted-foreground")}>
                  {r.values[c.key] ?? "n/a"}
                </td>
              ))}
              {showBack && (
                <td className={cn(num, r.back === undefined && "text-muted-foreground")}>{r.back ?? "n/a"}</td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
