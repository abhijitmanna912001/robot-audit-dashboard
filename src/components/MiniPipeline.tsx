import type { PipelineStage, StageState } from "@/lib/pipeline";
import { cn } from "@/lib/utils";

// Four small dots, one per stage, for the command rows. Status colours only. Server-safe.
const dot: Record<StageState, string> = {
  passed: "bg-status-completed-accent",
  failed: "bg-status-rejected-accent",
  error: "bg-status-failed-accent",
  not_reached: "border border-muted-foreground/60",
  dry_run: "bg-status-info-accent ring-2 ring-status-info-accent/30",
  running: "bg-status-info-accent",
};

const word: Record<StageState, string> = {
  passed: "passed",
  failed: "failed",
  error: "error",
  not_reached: "not reached",
  dry_run: "dry run",
  running: "running",
};

export function MiniPipeline({ stages }: { stages: readonly PipelineStage[] }) {
  const label = `Pipeline: ${stages.map((s) => `${s.stage} ${word[s.state]}`).join(", ")}`;
  return (
    <span role="img" aria-label={label} title={label} className="flex shrink-0 items-center gap-1.5">
      {stages.map((s) => (
        <span key={s.stage} aria-hidden="true" className={cn("size-2 rounded-full", dot[s.state])} />
      ))}
    </span>
  );
}
