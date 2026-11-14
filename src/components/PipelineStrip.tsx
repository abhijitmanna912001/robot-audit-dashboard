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
import { activeStage, type PipelineStage, type StageName, type StageState } from "@/lib/pipeline";
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

// Circles are opaque (bg-raised) so a connector never shows through them.
const circle: Record<Tone, string> = {
  ok: "border-status-completed-accent text-status-completed-accent",
  blocked: "border-status-rejected-accent text-status-rejected-accent",
  broke: "border-status-failed-accent text-status-failed-accent",
  info: "border-status-info-accent text-status-info-accent",
  idle: "border-dashed border-muted-foreground/50 text-muted-foreground",
};

// Connector colour = the earlier step's colour. It is dashed and muted when the next step was
// not reached (or the earlier one is idle), so a line only looks solid where the flow went.
const connector: Record<Exclude<Tone, "idle">, string> = {
  ok: "bg-status-completed-accent",
  blocked: "bg-status-rejected-accent",
  broke: "bg-status-failed-accent",
  info: "bg-status-info-accent",
};
const DASHED = "border-t-2 border-dashed border-muted-foreground/50";

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
  const active = activeStage(stages);
  return (
    <ol
      aria-label="Pipeline"
      className="grid grid-cols-2 gap-x-3 gap-y-3 rounded-ui-lg bg-surface px-3 py-3 sm:grid-cols-4"
    >
      {stages.map(({ stage, state }, i) => {
        const tone = toneOf(stage, state);
        const Icon = stageIcon[stage];
        const next = stages[i + 1];
        const solid = tone !== "idle" && next !== undefined && next.state !== "not_reached";
        return (
          <li
            key={stage}
            aria-current={stage === active ? "step" : undefined}
            className="relative flex min-w-0 items-center gap-2 sm:flex-col sm:gap-1.5 sm:text-center"
          >
            {next && (
              <span
                aria-hidden="true"
                style={{ width: "calc(100% - 20px)" }}
                className={cn(
                  "absolute top-[15px] left-[calc(50%+1rem)] hidden sm:block",
                  solid ? `h-0.5 ${connector[tone as Exclude<Tone, "idle">]}` : DASHED,
                )}
              />
            )}
            <span
              className={cn(
                "relative flex size-8 shrink-0 items-center justify-center rounded-full border-2 bg-raised transition-colors duration-150",
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
