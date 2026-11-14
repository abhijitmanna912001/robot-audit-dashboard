import {
  Check,
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

const circle: Record<Tone, string> = {
  ok: "border-status-completed-accent bg-status-completed-accent/15 text-status-completed-accent",
  blocked: "border-status-rejected-accent bg-status-rejected-accent/15 text-status-rejected-accent",
  broke: "border-status-failed-accent bg-status-failed-accent/15 text-status-failed-accent",
  info: "border-status-info-accent bg-status-info-accent/15 text-status-info-accent",
  idle: "border-dashed border-border bg-surface text-muted-foreground",
};

function StateGlyph({ state }: { state: StageState }) {
  const cls = "size-3 shrink-0";
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

// One horizontal stepper: a continuous line behind four circles (sm and up). Below sm it is
// a compact 2x2 grid with no line. The state word is always written out.
export function PipelineStrip({ stages }: { stages: PipelineStage[] }) {
  return (
    <ol
      aria-label="Pipeline"
      className="relative grid grid-cols-2 gap-x-3 gap-y-3 rounded-lg bg-surface px-3 py-3 sm:grid-cols-4"
    >
      <span
        aria-hidden="true"
        className="absolute top-7 right-[12.5%] left-[12.5%] hidden h-px bg-border sm:block"
      />
      {stages.map(({ stage, state }) => {
        const tone = toneOf(stage, state);
        const Icon = stageIcon[stage];
        return (
          <li
            key={stage}
            className="relative flex min-w-0 items-center gap-2 sm:flex-col sm:gap-1.5 sm:text-center"
          >
            <span
              className={cn(
                "flex size-8 shrink-0 items-center justify-center rounded-full border transition-colors duration-150",
                circle[tone],
              )}
            >
              <Icon aria-hidden="true" className="size-4" />
            </span>
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-foreground">
                {stageLabel[stage]}
              </span>
              <span
                className={cn(
                  "flex items-center gap-1 text-xs sm:justify-center",
                  tone === "idle" ? "text-muted-foreground" : "text-foreground/80",
                )}
              >
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
