// DRAFT TYPES: schemas are not agreed yet. Expect these to change.

export const COMMAND_STATUSES = [
  "received",
  "approved",
  "rejected",
  "executing",
  "completed",
  "failed",
] as const;
export type CommandStatus = (typeof COMMAND_STATUSES)[number];

export type Command = {
  id: string;
  text: string;
  status: CommandStatus;
  planId: string | null; // null until the worker generates a plan
  claimedBy: string | null; // worker instance id
  claimedAt: string | null; // ISO 8601
  createdAt: string; // ISO 8601
  updatedAt: string; // ISO 8601
};

// DRAFT: critic request contract, not agreed yet.
export type ActionType = "set_joint" | "move_forward" | "stop";

// DRAFT
export type CriticAction = {
  actionId: string;
  type: ActionType;
  target?: string;
  value?: number;
  unit?: string;
  reason: string;
};

// DRAFT
export type CriticRequest = {
  commandId: string;
  planId: string;
  planHash: string; // DRAFT: "sha256:<hex>" over canonical actions, see docs/schema-draft.md
  actions: CriticAction[];
};

// DRAFT
export type CriticVerdict = "approve" | "reject";

// DRAFT: critic response contract, not agreed yet.
export type CriticResponse = {
  commandId: string;
  planId: string;
  // DRAFT: the hash the critic recomputed itself. Omitted when none exists
  // (401, 400, 500, or a hashing error).
  planHash?: string;
  verdict: CriticVerdict;
  reason: string;
  triggeredRules: string[];
};

export const EVENT_TYPES = [
  "command_received",
  "plan_generated",
  "validator_result",
  "critic_result",
  "execution_started",
  "execution_completed",
  "execution_failed",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export type Json =
  | string
  | number
  | boolean
  | null
  | Json[]
  | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export type AuditEvent = {
  id: string;
  commandId: string;
  eventType: EventType;
  message: string;
  payload: JsonObject; // shapes per event type: docs/schema-draft.md "Event payloads"
  createdAt: string; // ISO 8601
};
