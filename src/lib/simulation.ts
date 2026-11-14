// Simulation runs, display only. Pure. docs/schema-draft.md "Proposed simulation payloads"
// (PROPOSED: the worker does not emit these yet). Payloads are untrusted, so every reader takes
// `unknown`, caps strings through payload.ts, drops anything malformed and never throws.
//
// An execution event is exactly one of three modes:
//   dry_run    dryRun is strictly true
//   simulation mode is exactly "simulation" and dryRun is strictly false
//   unknown    anything else (the worker only writes the two above, so nothing here may be
//              presented as a real execution)
import { asRecord, finite, text } from "./payload.ts";

export const JOINTS = ["_1", "_2", "_3", "_4", "_5", "_6"] as const;
export type JointName = (typeof JOINTS)[number];

export type Snapshot = { readAt: string; joints: Partial<Record<JointName, number>> };
export type StopState = "confirmed" | "unconfirmed" | "not_needed";

export type SimulationDetails = {
  mode: "simulation";
  summary?: string;
  error?: { kind?: string; detail?: string };
  stop?: StopState;
  toleranceRad?: number;
  start?: Snapshot;
  target?: Snapshot;
  restored?: Snapshot;
  lastTelemetry?: Snapshot;
};

export type ExecMode = "dry_run" | "simulation" | "unknown";

const MAX_TEXT = 200;
const MAX_KIND = 80;

// Human labels for simulation error kinds. Used ONLY for simulation events (isSimulation); planner and
// critic kinds keep their own table in kind-label.ts.
// TODO: reconcile this table with the worker's error-kind allowlist when PR #19 merges. Until then the
// kinds below are the proposed ones, and any other kind falls back to sentence case.
const SIM_KIND_LABELS: Readonly<Record<string, string>> = {
  overall_timeout: "Simulation timed out",
  connection_failed: "Could not connect to the simulation",
  gate_unconfirmed: "Movement blocked by a safety gate",
  interrupted: "Interrupted",
  interrupted_write: "Interrupted while recording",
  session_crashed: "Simulation session crashed",
};
const MAX_KIND_LABEL = 40;

// Known kind: its label. Other string: snake_case to sentence case ("telemetry_invalid" ->
// "Telemetry invalid"), capped at 40 characters. Empty, blank or non-string: undefined.
export function simulationKindLabel(kind: unknown): string | undefined {
  if (typeof kind !== "string") return undefined;
  const key = kind.trim();
  if (Object.hasOwn(SIM_KIND_LABELS, key)) return SIM_KIND_LABELS[key];
  const t = text(kind, MAX_KIND_LABEL);
  if (t === undefined) return undefined;
  const words = t.replace(/[_\s]+/g, " ").trim();
  return words === "" ? undefined : words.charAt(0).toUpperCase() + words.slice(1).toLowerCase();
}

export function isSimulation(payload: unknown): boolean {
  const p = asRecord(payload);
  return p?.mode === "simulation" && p.dryRun === false;
}

export function executionMode(payload: unknown): ExecMode {
  if (asRecord(payload)?.dryRun === true) return "dry_run";
  return isSimulation(payload) ? "simulation" : "unknown";
}

const UTC_Z = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$/;

// Used only when readAt is a real UTC timestamp ending in "Z" and every present joint is a finite
// number. Only _1.._6 are kept. A snapshot with no usable joint is not a snapshot.
export function readSnapshot(v: unknown): Snapshot | undefined {
  const s = asRecord(v);
  const readAt = s?.readAt;
  if (typeof readAt !== "string" || !UTC_Z.test(readAt)) return undefined;
  const d = new Date(readAt);
  // Rejects dates that only parse by rolling over (for example 31 February).
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 19) !== readAt.slice(0, 19)) return undefined;
  const raw = asRecord(s?.joints);
  if (!raw) return undefined;
  const joints: Partial<Record<JointName, number>> = {};
  for (const j of JOINTS) {
    if (!Object.hasOwn(raw, j)) continue;
    const n = finite(raw[j]);
    if (n === undefined) return undefined;
    joints[j] = n;
  }
  return Object.keys(joints).length === 0 ? undefined : { readAt, joints };
}

// undefined unless the payload is a simulation payload (see isSimulation).
export function readSimulation(payload: unknown): SimulationDetails | undefined {
  if (!isSimulation(payload)) return undefined;
  const p = asRecord(payload) as Record<string, unknown>;
  const err = asRecord(p.error);
  const error = err ? { kind: text(err.kind, MAX_KIND), detail: text(err.detail, MAX_TEXT) } : undefined;
  const tel = asRecord(p.telemetry);
  return {
    mode: "simulation",
    summary: text(p.summary, MAX_TEXT),
    error: error && (error.kind !== undefined || error.detail !== undefined) ? error : undefined,
    stop: p.stop === "confirmed" || p.stop === "unconfirmed" || p.stop === "not_needed" ? p.stop : undefined,
    toleranceRad: finite(p.toleranceRad),
    start: readSnapshot(tel?.start),
    target: readSnapshot(tel?.target),
    restored: readSnapshot(tel?.restored),
    lastTelemetry: readSnapshot(p.lastTelemetry),
  };
}

export type RestoreCheck =
  | { status: "restored" | "outside"; maxDelta: number; toleranceRad: number }
  | { status: "not_verified"; reason: "no_start" | "no_restored" | "no_tolerance" | "no_common_joints" };

const validTolerance = (t: unknown): t is number => typeof t === "number" && Number.isFinite(t) && t > 0;

// "Arm restored" is claimed only when both snapshots exist, the tolerance is a finite positive
// number, and every joint present in both differs by at most the tolerance.
export function restoreCheck(start?: Snapshot, restored?: Snapshot, toleranceRad?: unknown): RestoreCheck {
  if (!start) return { status: "not_verified", reason: "no_start" };
  if (!restored) return { status: "not_verified", reason: "no_restored" };
  if (!validTolerance(toleranceRad)) return { status: "not_verified", reason: "no_tolerance" };
  let maxDelta: number | undefined;
  for (const j of JOINTS) {
    const a = start.joints[j];
    const b = restored.joints[j];
    if (a === undefined || b === undefined) continue;
    const d = Math.abs(b - a);
    if (maxDelta === undefined || d > maxDelta) maxDelta = d;
  }
  if (maxDelta === undefined) return { status: "not_verified", reason: "no_common_joints" };
  return { status: maxDelta <= toleranceRad ? "restored" : "outside", maxDelta, toleranceRad };
}

// Joints whose target differs from start by more than the tolerance (or by anything, when the
// tolerance is missing or invalid).
export function movedJoints(start?: Snapshot, target?: Snapshot, toleranceRad?: unknown): ReadonlySet<JointName> {
  const out = new Set<JointName>();
  if (!start || !target) return out;
  const limit = validTolerance(toleranceRad) ? toleranceRad : 0;
  for (const j of JOINTS) {
    const a = start.joints[j];
    const b = target.joints[j];
    if (a !== undefined && b !== undefined && Math.abs(b - a) > limit) out.add(j);
  }
  return out;
}

// rad with 3 decimals; "-0.000" is shown as "0.000".
export function formatRad(n: number): string {
  const s = n.toFixed(3);
  return s === "-0.000" ? "0.000" : s;
}

// restored - start, signed.
export function formatDelta(restored: number, start: number): string {
  const s = formatRad(restored - start);
  return s === "0.000" || s.startsWith("-") ? s : `+${s}`;
}

export function formatTolerance(n: number): string {
  return String(Number(n.toPrecision(3)));
}

// ---- Latest execution and its outcome -----------------------------------------------------

type EventLike = { eventType: unknown; payload: unknown };
export type Latest = { phase: "running" | "completed" | "failed"; mode: ExecMode; payload: unknown };

// The latest terminal execution event wins; otherwise the latest started event. Same precedence
// as derivePipeline.
export function latestExecution(events: unknown): Latest | undefined {
  if (!Array.isArray(events)) return undefined;
  let terminal: Latest | undefined;
  let started: Latest | undefined;
  for (const e of events as EventLike[]) {
    const type = e?.eventType;
    if (type === "execution_started") started = { phase: "running", mode: executionMode(e.payload), payload: e.payload };
    else if (type === "execution_completed") terminal = { phase: "completed", mode: executionMode(e.payload), payload: e.payload };
    else if (type === "execution_failed") terminal = { phase: "failed", mode: executionMode(e.payload), payload: e.payload };
  }
  return terminal ?? started;
}

export function latestSimulation(events: unknown): SimulationDetails | undefined {
  try {
    const x = latestExecution(events);
    return x?.mode === "simulation" ? readSimulation(x.payload) : undefined;
  } catch {
    return undefined;
  }
}

export type OutcomeTone = "success" | "broke" | "running" | "warning";
export type ExecutionOutcome = { headline: string; stamp: string; tone: OutcomeTone; facts: string[] };

const NOT_VERIFIED_WHY: Record<Extract<RestoreCheck, { status: "not_verified" }>["reason"], string> = {
  no_start: "start reading missing",
  no_restored: "restored reading missing",
  no_tolerance: "tolerance missing or invalid",
  no_common_joints: "no joints to compare",
};

// Headline, stamp, tone and facts for a simulated run or for an execution whose mode was not
// recorded. undefined for dry runs (and for a failed or running run of unknown mode), which keep
// their existing wording. One source for summarize(), verdictStamp(), verdictFacts() and bannerTone(),
// so the text, stamp and colour always agree.
export function executionOutcome(events: unknown): ExecutionOutcome | undefined {
  try {
    const x = latestExecution(events);
    if (!x || x.mode === "dry_run") return undefined;
    if (x.mode === "unknown") {
      // No claim that anything moved or did not move.
      return x.phase === "completed"
        ? { headline: "Execution finished: mode not recorded", stamp: "MODE NOT RECORDED", tone: "warning", facts: [] }
        : undefined;
    }
    const sim = readSimulation(x.payload);
    if (!sim) return undefined;

    if (x.phase === "running") {
      return { headline: "Executing (simulation)", stamp: "RUNNING · SIMULATED", tone: "running", facts: [] };
    }

    if (x.phase === "completed") {
      const c = restoreCheck(sim.start, sim.restored, sim.toleranceRad);
      if (c.status === "not_verified") {
        return {
          headline: "Simulated move completed: restore not verified",
          stamp: "SIMULATED · RESTORE UNVERIFIED",
          tone: "warning",
          facts: [`restore not verified: ${NOT_VERIFIED_WHY[c.reason]}`],
        };
      }
      if (c.status === "restored") {
        return {
          headline: "Simulated move completed",
          stamp: "APPROVED · SIMULATED",
          tone: "success",
          facts: [`arm restored within ${formatTolerance(c.toleranceRad)} rad`],
        };
      }
      return {
        headline: "Simulated move completed: arm not restored",
        stamp: "SIMULATED · NOT RESTORED",
        tone: "warning",
        facts: [`largest difference from start ${formatRad(c.maxDelta)} rad, tolerance ${formatTolerance(c.toleranceRad)} rad`],
      };
    }

    // failed
    const label = simulationKindLabel(sim.error?.kind);
    const detail = sim.error?.detail === undefined ? [] : [`Detail: ${sim.error.detail}`];
    switch (sim.stop) {
      case "confirmed":
      case "not_needed":
        return {
          headline: label ? `Simulation failed: ${label}` : "Simulation failed",
          stamp: "FAILED · SIMULATED",
          tone: "broke",
          facts: [sim.stop === "confirmed" ? "simulation confirmed stopped" : "no stop was needed", ...detail],
        };
      case "unconfirmed":
        return {
          headline: "Simulation failed: arm state uncertain",
          stamp: "FAILED · ARM STATE UNCERTAIN",
          tone: "warning",
          facts: ["Manual check needed. Nothing was retried.", ...(label ? [`Cause: ${label}`] : []), ...detail],
        };
      default:
        return {
          headline: "Simulation failed: stop not recorded",
          stamp: "FAILED · ARM STATE UNCERTAIN",
          tone: "warning",
          facts: [...(label ? [`Cause: ${label}`] : []), ...detail],
        };
    }
  } catch {
    return undefined;
  }
}

// ---- Telemetry table data -----------------------------------------------------------------

export type TelemetryColumn = "start" | "target" | "restored" | "last";
export type TelemetryRow = {
  joint: JointName;
  values: Partial<Record<TelemetryColumn, string>>;
  back?: string; // restored minus start
  moved: boolean;
};
export type Telemetry = {
  columns: { key: TelemetryColumn; label: string; readAt: string }[];
  showBack: boolean;
  rows: TelemetryRow[];
};

const COLUMN_LABEL: Record<TelemetryColumn, string> = {
  start: "Start",
  target: "Target",
  restored: "Restored",
  last: "Last reading",
};

// undefined when there is no snapshot to show.
export function telemetryRows(sim: SimulationDetails | undefined): Telemetry | undefined {
  if (!sim) return undefined;
  const snaps: Record<TelemetryColumn, Snapshot | undefined> = {
    start: sim.start,
    target: sim.target,
    restored: sim.restored,
    last: sim.lastTelemetry,
  };
  const keys = (["start", "target", "restored", "last"] as const).filter((k) => snaps[k] !== undefined);
  if (keys.length === 0) return undefined;
  const moved = movedJoints(sim.start, sim.target, sim.toleranceRad);
  const showBack = sim.start !== undefined && sim.restored !== undefined;
  const rows = JOINTS.map((joint): TelemetryRow => {
    const values: Partial<Record<TelemetryColumn, string>> = {};
    for (const k of keys) {
      const v = snaps[k]?.joints[joint];
      if (v !== undefined) values[k] = formatRad(v);
    }
    const a = sim.start?.joints[joint];
    const b = sim.restored?.joints[joint];
    return {
      joint,
      values,
      back: showBack && a !== undefined && b !== undefined ? formatDelta(b, a) : undefined,
      moved: moved.has(joint),
    };
  });
  return {
    columns: keys.map((key) => ({ key, label: COLUMN_LABEL[key], readAt: (snaps[key] as Snapshot).readAt })),
    showBack,
    rows,
  };
}
