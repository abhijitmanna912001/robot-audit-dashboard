// Expandable "why" details for one audit event. Display only.
// Everything is rendered as React text (escaped). Payloads are untrusted, see lib/payload.ts.
import type { AuditEvent } from "@/types/draft";
import {
  readCritic,
  readExecution,
  readPlan,
  readPlanFailure,
  readRaw,
  readSource,
  readValidator,
} from "@/lib/payload";
import { CopyButton } from "./CopyButton";

// `copy` adds a copy button next to a short single-line value (the plan hash).
type Row = { label: string; value: string | string[]; copy?: boolean };

function add(rows: Row[], label: string, value: string | string[] | undefined, copy = false) {
  if (value === undefined || (Array.isArray(value) && value.length === 0)) return;
  rows.push({ label, value, copy: copy && typeof value === "string" });
}

const orDash = (v: string | number | undefined) => (v === undefined ? "?" : String(v));

function build(e: Pick<AuditEvent, "eventType" | "payload">): {
  rows: Row[];
  dryRun: boolean;
  raw?: string;
} {
  const rows: Row[] = [];
  // eventType is a closed union in the types, but rows are untrusted at runtime.
  switch (e.eventType as string) {
    case "command_received":
      add(rows, "source", readSource(e.payload));
      return { rows, dryRun: false };
    case "plan_generated": {
      const failure = readPlanFailure(e.payload);
      if (failure) {
        add(rows, "error kind", failure.kind);
        add(rows, "error detail", failure.detail);
        return { rows, dryRun: false };
      }
      const p = readPlan(e.payload);
      add(rows, "planId", p.planId);
      add(rows, "planHash", p.planHash, true);
      const lines = p.actions.map((a) => {
        const base = `${orDash(a.actionId)}: ${orDash(a.target)} = ${orDash(a.value)} ${orDash(a.unit)}`;
        return a.reason ? `${base} (${a.reason})` : base;
      });
      if (p.extraActions > 0) lines.push(`+${p.extraActions} more`);
      add(rows, "actions", lines);
      return { rows, dryRun: false };
    }
    case "validator_result": {
      const v = readValidator(e.payload);
      add(rows, "outcome", v.outcome);
      add(rows, "validatorVersion", v.validatorVersion);
      const lines = v.errors.map((x) => {
        const where =
          x.actionId === null && x.field === null
            ? "plan-level"
            : `action ${x.actionId ?? "?"}, field ${x.field ?? "?"}`;
        return `${orDash(x.ruleId)} (${where})${x.message ? `: ${x.message}` : ""}`;
      });
      if (v.extraErrors > 0) lines.push(`+${v.extraErrors} more`);
      add(rows, "errors", lines);
      return { rows, dryRun: false };
    }
    case "critic_result": {
      const c = readCritic(e.payload);
      add(rows, "outcome", c.outcome);
      add(rows, "reason", c.reason);
      const rules = [...c.triggeredRules];
      if (c.extraRules > 0) rules.push(`+${c.extraRules} more`);
      add(rows, "triggeredRules", rules);
      add(rows, "planHash", c.planHash, true);
      if (c.responsePlanHash !== c.planHash) add(rows, "critic planHash", c.responsePlanHash, true);
      add(rows, "error kind", c.error?.kind);
      add(rows, "HTTP status", c.error?.httpStatus === undefined ? undefined : String(c.error.httpStatus));
      add(rows, "error detail", c.error?.detail);
      return { rows, dryRun: false };
    }
    case "execution_started":
    case "execution_completed":
    case "execution_failed": {
      const x = readExecution(e.payload);
      add(rows, "summary", x.summary);
      add(rows, "error", x.error);
      return { rows, dryRun: x.dryRun === true };
    }
    default:
      return { rows, dryRun: false, raw: readRaw(e.payload) };
  }
}

export function EventDetails({ event }: { event: Pick<AuditEvent, "eventType" | "payload"> }) {
  const { rows, dryRun, raw } = build(event);

  if (raw !== undefined) {
    return (
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer select-none text-muted-foreground hover:text-foreground">
          Raw payload
        </summary>
        <pre className="mt-1.5 whitespace-pre-wrap wrap-anywhere rounded-lg border border-border bg-muted/40 p-2 font-mono">
          {raw}
        </pre>
      </details>
    );
  }
  if (rows.length === 0 && !dryRun) return null;

  return (
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer select-none text-muted-foreground hover:text-foreground">
        Details
        {dryRun && (
          <span className="ml-2 rounded-full bg-status-info px-2 py-0.5 font-medium uppercase tracking-wide text-status-info-foreground">
            dry run
          </span>
        )}
      </summary>
      <dl className="mt-1.5 space-y-1.5 rounded-lg border border-border bg-muted/40 p-2">
        {rows.map((r) => (
          <div key={r.label} className="min-w-0">
            <dt className="text-muted-foreground">{r.label}</dt>
            <dd className="wrap-anywhere flex min-w-0 items-start gap-1 font-mono">
              {Array.isArray(r.value) ? (
                <ul className="min-w-0 list-inside list-disc">
                  {r.value.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
              ) : (
                <>
                  <span className="min-w-0">{r.value}</span>
                  {r.copy && <CopyButton value={r.value} label={r.label} />}
                </>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
