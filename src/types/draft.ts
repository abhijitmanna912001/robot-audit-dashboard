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

export type AuditEvent = {
  id: string;
  commandId: string;
  eventType: string; // free-form for now
  message: string;
  timestamp: string; // ISO 8601
};
