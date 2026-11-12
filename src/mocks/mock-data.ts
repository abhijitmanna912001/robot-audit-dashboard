// MOCK DATA: delete this file (and its import in src/app/page.tsx) once
// Supabase is wired up.
import type { AuditEvent, Command } from "@/types/draft";

export const mockCommands: Command[] = [
  { id: "c1", text: "Pick up the red cube", status: "completed", createdAt: "2026-11-14T10:00:00Z" },
  { id: "c2", text: "Move arm to the left bin", status: "executing", createdAt: "2026-11-14T10:05:00Z" },
  { id: "c3", text: "Stack three blocks", status: "approved", createdAt: "2026-11-14T10:08:00Z" },
  { id: "c4", text: "Drive forward 50 metres at full speed", status: "rejected", createdAt: "2026-11-14T10:10:00Z" },
  { id: "c5", text: "Open the gripper", status: "failed", createdAt: "2026-11-14T10:12:00Z" },
  { id: "c6", text: "Return to home position", status: "received", createdAt: "2026-11-14T10:15:00Z" },
];

export const mockAuditEvents: AuditEvent[] = [
  { id: "e1", commandId: "c1", eventType: "received", message: "Command received", timestamp: "2026-11-14T10:00:01Z" },
  { id: "e2", commandId: "c1", eventType: "critic_pass", message: "Safety critic approved the plan", timestamp: "2026-11-14T10:00:03Z" },
  { id: "e3", commandId: "c1", eventType: "validator_pass", message: "Validator accepted the plan", timestamp: "2026-11-14T10:00:04Z" },
  { id: "e4", commandId: "c1", eventType: "executing", message: "Simulation started", timestamp: "2026-11-14T10:00:05Z" },
  { id: "e5", commandId: "c1", eventType: "completed", message: "Simulation finished successfully", timestamp: "2026-11-14T10:00:20Z" },

  { id: "e6", commandId: "c2", eventType: "received", message: "Command received", timestamp: "2026-11-14T10:05:01Z" },
  { id: "e7", commandId: "c2", eventType: "critic_pass", message: "Safety critic approved the plan", timestamp: "2026-11-14T10:05:03Z" },
  { id: "e8", commandId: "c2", eventType: "executing", message: "Simulation started", timestamp: "2026-11-14T10:05:05Z" },

  { id: "e9", commandId: "c3", eventType: "received", message: "Command received", timestamp: "2026-11-14T10:08:01Z" },
  { id: "e10", commandId: "c3", eventType: "critic_pass", message: "Safety critic approved the plan", timestamp: "2026-11-14T10:08:03Z" },

  { id: "e11", commandId: "c4", eventType: "received", message: "Command received", timestamp: "2026-11-14T10:10:01Z" },
  { id: "e12", commandId: "c4", eventType: "critic_fail", message: "Safety critic flagged unsafe speed", timestamp: "2026-11-14T10:10:03Z" },
  { id: "e13", commandId: "c4", eventType: "rejected", message: "Command rejected, nothing executed", timestamp: "2026-11-14T10:10:04Z" },

  { id: "e14", commandId: "c5", eventType: "received", message: "Command received", timestamp: "2026-11-14T10:12:01Z" },
  { id: "e15", commandId: "c5", eventType: "validator_pass", message: "Validator accepted the plan", timestamp: "2026-11-14T10:12:04Z" },
  { id: "e16", commandId: "c5", eventType: "executing", message: "Simulation started", timestamp: "2026-11-14T10:12:05Z" },
  { id: "e17", commandId: "c5", eventType: "failed", message: "Gripper actuator error in simulation", timestamp: "2026-11-14T10:12:09Z" },

  { id: "e18", commandId: "c6", eventType: "received", message: "Command received", timestamp: "2026-11-14T10:15:01Z" },
];
