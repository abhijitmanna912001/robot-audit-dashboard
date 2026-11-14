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
import { activeStage, STATE_WORD, type PipelineStage, type StageName, type StageState } from "@/lib/pipeline";
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

// Opaque stage tiles keep connectors clear of their icons.
const circle: Record<Tone, string> = {
  ok: "border-status-completed-accent text-status-completed-accent",
  blocked: "border-status-rejected-accent text-status-rejected-accent",
  broke: "border-status-failed-accent text-status-failed-accent",
  info: "border-status-info-accent text-status-info-accent",
  idle: "border-dashed border-input text-muted-foreground",
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
  idle: "to-input",
};
const DASHED = "border-t border-dashed border-input";

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
      return <Loader aria-hidden="true" className={cls} />;
    default:
      return <CircleDashed aria-hidden="true" className={cls} />;
  }
}

// Wide case files show a connected chain; narrow ones use a compact 2x2 grid.
// The state word is always written out.
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
      className="grid min-w-0 grid-cols-2 gap-x-4 gap-y-5 @case:grid-cols-4"
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
            className="relative flex min-w-0 items-start gap-2 @case:flex-col @case:gap-3 @case:text-left"
          >
            {next && (
              <span
                aria-hidden="true"
                style={{ width: "calc(100% - 32px)" }}
                className={cn(
                  "absolute top-4 left-10 hidden @case:block",
                  solid
                    ? `h-px rounded-full bg-linear-to-r ${connectorFrom[tone as Exclude<Tone, "idle">]} ${connectorTo[nextTone]}`
                    : DASHED,
                )}
              />
            )}
            <span
              className={cn(
                "relative flex size-8 shrink-0 items-center justify-center rounded-ui-sm border bg-surface transition-colors duration-150",
                circle[tone],
              )}
            >
              <Icon aria-hidden="true" className="size-4" />
            </span>
            <span className="min-w-0">
              <span className="block text-[13px] leading-5 font-semibold text-foreground">
                {stageLabel[stage]}
              </span>
              <span
                className={cn(
                  "flex items-center gap-1 text-xs leading-5",
                  tone === "idle" ? "text-muted-foreground" : "text-foreground",
                )}
              >
                <StateGlyph state={state} />
                {STATE_WORD[state]}
              </span>
              {details[stage] && (
                <span className="mt-1 block text-xs leading-5 wrap-anywhere text-muted-foreground">
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
