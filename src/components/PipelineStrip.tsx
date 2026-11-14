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
  ok: "border-status-completed-accent text-status-completed-accent shadow-[0_0_16px_-2px_var(--status-completed-accent)]",
  blocked: "border-status-rejected-accent text-status-rejected-accent shadow-[0_0_16px_-2px_var(--status-rejected-accent)]",
  broke: "border-status-failed-accent text-status-failed-accent shadow-[0_0_16px_-2px_var(--status-failed-accent)]",
  info: "border-status-info-accent text-status-info-accent shadow-[0_0_16px_-2px_var(--status-info-accent)]",
  idle: "border-dashed border-muted-foreground/50 text-muted-foreground",
};

// Connector colour = the earlier step's colour. It is dashed and muted when the next step was
// not reached (or the earlier one is idle), so a line only looks solid where the flow went.
const connectorFrom: Record<Exclude<Tone, "idle">, string> = {
  ok: "from-status-completed-accent",
  blocked: "from-status-rejected-accent",
  broke: "from-status-failed-accent",
  info: "from-status-info-accent",
};
const connectorTo: Record<Tone, string> = {
  ok: "to-status-completed-accent",
  blocked: "to-status-rejected-accent",
  broke: "to-status-failed-accent",
  info: "to-status-info-accent",
  idle: "to-muted-foreground/50",
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
export function PipelineStrip({
  stages,
  details = {},
}: {
  stages: PipelineStage[];
  details?: Partial<Record<StageName, string>>;
}) {
  const active = activeStage(stages);
  return (
    <ol
      aria-label="Pipeline"
      className="surface-card grid grid-cols-2 gap-x-3 gap-y-4 rounded-ui-lg px-3 py-4 sm:grid-cols-4"
    >
      {stages.map(({ stage, state }, i) => {
        const tone = toneOf(stage, state);
        const Icon = stageIcon[stage];
        const next = stages[i + 1];
        const solid = tone !== "idle" && next !== undefined && next.state !== "not_reached";
        const nextTone = next ? toneOf(next.stage, next.state) : "idle";
        return (
          <li
            key={stage}
            aria-current={stage === active ? "step" : undefined}
            className="relative flex min-w-0 items-center gap-2 sm:flex-col sm:gap-1.5 sm:text-center"
          >
            {next && (
              <span
                aria-hidden="true"
                style={{ width: "calc(100% - 28px)" }}
                className={cn(
                  "absolute top-[19px] left-[calc(50%+1.5rem)] hidden sm:block",
                  solid
                    ? `anim-line h-0.5 rounded-full bg-linear-to-r ${connectorFrom[tone as Exclude<Tone, "idle">]} ${connectorTo[nextTone]}`
                    : DASHED,
                )}
              />
            )}
            <span
              className={cn(
                "relative flex size-10 shrink-0 items-center justify-center rounded-full border-2 bg-raised transition-colors duration-150",
                circle[tone],
              )}
            >
              <Icon aria-hidden="true" className="size-[18px]" />
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
              {details[stage] && (
                <span className="mt-0.5 block wrap-anywhere font-mono text-[11px] leading-snug text-muted-foreground">
                  {details[stage]}
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
