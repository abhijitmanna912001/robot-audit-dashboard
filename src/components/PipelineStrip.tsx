import {
  Check,
  ChevronRight,
  CircleDashed,
  ClipboardList,
  FlaskConical,
  Loader,
  Play,
  Scale,
  ShieldCheck,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import type { PipelineStage, StageName, StageState } from "@/lib/pipeline";
import { cn } from "@/lib/utils";

const stageIcon: Record<StageName, LucideIcon> = {
  planned: ClipboardList,
  validator: ShieldCheck,
  critic: Scale,
  execution: Play,
};

const stageLabel: Record<StageName, string> = {
  planned: "Planned",
  validator: "Validator",
  critic: "Critic",
  execution: "Execution",
};

const stateLabel: Record<StageState, string> = {
  passed: "passed",
  failed: "failed",
  error: "error",
  not_reached: "not reached",
  dry_run: "dry run",
  running: "running",
};

// Red = blocked (validator fail, critic reject), amber = something broke (errors and a
// failed execution). The state word is always shown, so colour is never the only cue.
type Tone = "ok" | "blocked" | "broke" | "idle" | "info";

function toneOf(stage: StageName, state: StageState): Tone {
  switch (state) {
    case "passed":
      return "ok";
    case "failed":
      return stage === "execution" ? "broke" : "blocked";
    case "error":
      return "broke";
    case "dry_run":
    case "running":
      return "info";
    default:
      return "idle";
  }
}

const toneStyles: Record<Tone, string> = {
  ok: "border-status-completed-accent/40 bg-status-completed-accent/10",
  blocked: "border-status-rejected-accent/60 bg-status-rejected-accent/15",
  broke: "border-status-failed-accent/60 bg-status-failed-accent/15",
  info: "border-status-info-accent/50 bg-status-info-accent/10",
  idle: "border-dashed border-border bg-transparent text-muted-foreground",
};

const toneIcon: Record<Tone, string> = {
  ok: "text-status-completed-accent",
  blocked: "text-status-rejected-accent",
  broke: "text-status-failed-accent",
  info: "text-status-info-accent",
  idle: "text-muted-foreground",
};

function StateGlyph({ state }: { state: StageState }) {
  const cls = "size-3.5 shrink-0";
  switch (state) {
    case "passed":
      return <Check aria-hidden="true" className={cls} />;
    case "failed":
      return <X aria-hidden="true" className={cls} />;
    case "error":
      return <TriangleAlert aria-hidden="true" className={cls} />;
    case "dry_run":
      return <FlaskConical aria-hidden="true" className={cls} />;
    case "running":
      return <Loader aria-hidden="true" className={cn(cls, "animate-spin")} />;
    default:
      return <CircleDashed aria-hidden="true" className={cls} />;
  }
}

export function PipelineStrip({ stages }: { stages: PipelineStage[] }) {
  return (
    <ol aria-label="Pipeline" className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
      {stages.map(({ stage, state }, i) => {
        const tone = toneOf(stage, state);
        const Icon = stageIcon[stage];
        return (
          <li
            key={stage}
            className={cn(
              "relative flex min-w-0 items-center gap-2 rounded-xl border px-3 py-2 text-xs transition-colors duration-150",
              toneStyles[tone],
            )}
          >
            {i < stages.length - 1 && (
              <ChevronRight
                aria-hidden="true"
                className={cn(
                  "absolute top-1/2 -right-3.5 size-3 -translate-y-1/2 text-muted-foreground",
                  i % 2 === 1 && "max-sm:hidden",
                )}
              />
            )}
            <Icon aria-hidden="true" className={cn("size-4 shrink-0", toneIcon[tone])} />
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-foreground">
                {stageLabel[stage]}
              </span>
              <span className={cn("flex items-center gap-1", tone === "idle" ? "" : "text-foreground/80")}>
                <StateGlyph state={state} />
                {stateLabel[state]}
              </span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
