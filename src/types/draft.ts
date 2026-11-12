// DRAFT TYPES: schemas are not agreed yet. Expect these to change.

export type CommandStatus =
  | "received"
  | "approved"
  | "rejected"
  | "executing"
  | "completed"
  | "failed";

export type Command = {
  id: string;
  text: string;
  status: CommandStatus;
  createdAt: string; // ISO 8601
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
  actions: CriticAction[];
};

// DRAFT
export type CriticVerdict = "approve" | "reject";

// DRAFT: critic response contract, not agreed yet.
export type CriticResponse = {
  commandId: string;
  planId: string;
  verdict: CriticVerdict;
  reason: string;
  triggeredRules: string[];
};

export type AuditEvent = {
  id: string;
  commandId: string;
  eventType: string; // free-form for now
  message: string;
  timestamp: string; // ISO 8601
};
