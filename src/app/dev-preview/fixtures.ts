// Fixtures for /dev-preview only. Nothing here touches the database.
// Shapes follow docs/schema-draft.md "Event payloads". The "malformed" scenario
// deliberately breaks them to show the timeline never crashes on untrusted payloads.
import type { AuditEvent, CommandStatus } from "@/types/draft";

export type Scenario = { title: string; status: CommandStatus; events: AuditEvent[] };

const PLAN_ID = "9d2e4a10-0000-0000-0000-000000000002";
const HASH = "sha256:" + "ab".repeat(32);
const OTHER_HASH = "sha256:" + "cd".repeat(32);

const plan = (value: number) => ({
  planId: PLAN_ID,
  planHash: HASH,
  actions: [
    {
      actionId: "a1",
      type: "set_joint",
      target: "_1",
      value,
      unit: "rad",
      reason: "user asked to rotate the first joint",
    },
  ],
});

let n = 0;
// `eventType` and `payload` are loosened on purpose so fixtures can be malformed.
function ev(
  scenario: string,
  eventType: string,
  message: string,
  payload: unknown,
): AuditEvent {
  n += 1;
  return {
    id: `${scenario}-${n}`,
    commandId: scenario,
    eventType: eventType as AuditEvent["eventType"],
    message,
    payload: payload as AuditEvent["payload"],
    createdAt: new Date(Date.UTC(2026, 10, 14, 10, 0, n)).toISOString(),
  };
}

const received = (s: string) => ev(s, "command_received", "Command received", { source: "dashboard" });
const planned = (s: string, value: number) =>
  ev(s, "plan_generated", "Plan generated", { plan: plan(value) });
const validatorPass = (s: string) =>
  ev(s, "validator_result", "Validator passed", {
    outcome: "pass",
    planId: PLAN_ID,
    validatorVersion: "0.1.0",
    errors: [],
  });
const criticApprove = (s: string) =>
  ev(s, "critic_result", "Critic approved", {
    outcome: "approve",
    planHash: HASH,
    response: {
      commandId: s,
      planId: PLAN_ID,
      planHash: HASH,
      verdict: "approve",
      reason: "No rules triggered.",
      triggeredRules: [],
    },
  });

export const scenarios: Scenario[] = [
  {
    title: "Approved dry run",
    status: "completed",
    events: [
      received("ok"),
      planned("ok", 0.5),
      validatorPass("ok"),
      criticApprove("ok"),
      ev("ok", "execution_started", "Execution started", { dryRun: true }),
      ev("ok", "execution_completed", "Execution completed", {
        dryRun: true,
        summary: "1 action simulated, robot not driven",
      }),
    ],
  },
  {
    title: "Validator rejection",
    status: "rejected",
    events: [
      received("val"),
      planned("val", 3),
      ev("val", "validator_result", "Validator failed", {
        outcome: "fail",
        planId: PLAN_ID,
        validatorVersion: "0.1.0",
        errors: [
          {
            ruleId: "JOINT_LIMIT_EXCEEDED",
            actionId: "a1",
            field: "value",
            message: "value 3 is outside the limits for _1",
          },
          {
            ruleId: "MAX_ACTIONS_EXCEEDED",
            actionId: null,
            field: null,
            message: "plan has too many actions",
          },
        ],
      }),
    ],
  },
  {
    title: "Critic rejection",
    status: "rejected",
    events: [
      received("crit"),
      planned("crit", 3),
      validatorPass("crit"),
      ev("crit", "critic_result", "Critic rejected", {
        outcome: "reject",
        planHash: HASH,
        response: {
          commandId: "crit",
          planId: PLAN_ID,
          planHash: OTHER_HASH,
          verdict: "reject",
          reason: "Action a1: value is outside the limits for _1 (-1.91986 to 1.91986 rad).",
          triggeredRules: ["joint_limit_exceeded", "plan_hash_mismatch"],
        },
      }),
    ],
  },
  {
    title: "Critic error",
    status: "rejected",
    events: [
      received("err"),
      planned("err", 0.5),
      validatorPass("err"),
      ev("err", "critic_result", "Critic unavailable", {
        outcome: "error",
        error: { kind: "http_status", httpStatus: 500, detail: "critic_error" },
      }),
    ],
  },
  {
    title: "Unsupported command",
    status: "failed",
    events: [
      received("unsup"),
      ev("unsup", "plan_generated", "Command not understood", {
        outcome: "error",
        error: { kind: "unsupported_command", detail: "The arm cannot move forward." },
      }),
    ],
  },
  {
    title: "Planner error",
    status: "failed",
    events: [
      received("perr"),
      ev("perr", "plan_generated", "Planning failed", {
        outcome: "error",
        error: { kind: "planner_error", detail: "The planner returned no usable plan." },
      }),
    ],
  },
  {
    title: "Validator fail + critic error",
    status: "rejected",
    events: [
      received("vce"),
      planned("vce", 3),
      ev("vce", "validator_result", "Validator failed", {
        outcome: "fail",
        planId: PLAN_ID,
        validatorVersion: "0.1.0",
        errors: [
          { ruleId: "JOINT_LIMIT_EXCEEDED", actionId: "a1", field: "value", message: "value 3 is outside the limits for _1" },
        ],
      }),
      ev("vce", "critic_result", "Critic unavailable", {
        outcome: "error",
        error: { kind: "timeout", detail: "critic did not answer in time" },
      }),
    ],
  },
  {
    title: "Malformed planner failure",
    status: "failed",
    events: [
      ev("badfail", "plan_generated", "error is a string", { outcome: "error", error: "boom" }),
      ev("badfail", "plan_generated", "kind and detail wrong types", { outcome: "error", error: { kind: 5, detail: { x: 1 } } }),
      ev("badfail", "plan_generated", "very long detail and HTML kind", {
        outcome: "error",
        error: { kind: "<b>kind</b>", detail: "z".repeat(900) },
      }),
    ],
  },
  {
    title: "Malformed payloads",
    status: "received",
    events: [
      ev("bad", "command_received", "source is a number", { source: 42 }),
      ev("bad", "plan_generated", "actions is a string, value is NaN-like", {
        plan: { planId: 7, actions: "nope" },
      }),
      ev("bad", "plan_generated", "mixed action items", {
        plan: { actions: [null, 5, { target: "_1", value: "0.5", unit: "rad" }, { target: "_2", value: 1e999, unit: "rad" }] },
      }),
      ev("bad", "validator_result", "errors is not an array", { outcome: "fail", errors: "boom" }),
      ev("bad", "critic_result", "response is an array", { outcome: "reject", response: [1, 2], error: "x" }),
      ev("bad", "execution_started", 'dryRun is the string "true"', { dryRun: "true" }),
      ev("bad", "execution_failed", "payload is null", null),
      ev("bad", "execution_completed", "payload is an array", [1, 2, 3]),
      ev("bad", "critic_result", "HTML and a very long string stay inert text", {
        outcome: "reject",
        response: {
          reason: '<img src=x onerror="alert(1)"> ' + "x".repeat(900),
          triggeredRules: Array.from({ length: 30 }, (_, i) => `rule_${i}`),
        },
      }),
      ev("bad", "something_new", "unknown event type", { note: "<b>raw</b>", blob: "y".repeat(3000) }),
    ],
  },
];
