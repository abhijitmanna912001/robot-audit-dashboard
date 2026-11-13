import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeCommands, mergeEvents } from "./merge.ts";
import { getMarker } from "./outcome.ts";
import type { AuditEvent, Command } from "../types/draft.ts";

const cmd = (id: string, created: string, updated = created, status: Command["status"] = "received"): Command => ({
  id, text: id, status, planId: null, claimedBy: null, claimedAt: null, createdAt: created, updatedAt: updated,
});
const ev = (id: string, created: string): AuditEvent => ({
  id, commandId: "c", eventType: "plan_generated", message: id, payload: {}, createdAt: created,
});

test("mergeCommands dedupes, sorts newest first, and is idempotent", () => {
  const a = cmd("a", "2026-01-01T00:00:00Z");
  const b = cmd("b", "2026-01-02T00:00:00Z");
  const once = mergeCommands([a], [b, a]);
  assert.deepEqual(once.map((c) => c.id), ["b", "a"]);
  assert.deepEqual(mergeCommands(once, once), once);
});

test("mergeCommands: stale snapshot does not overwrite a newer status", () => {
  const newer = cmd("a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z", "approved");
  const stale = cmd("a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "received");
  assert.equal(mergeCommands([newer], [stale])[0].status, "approved");
  assert.equal(mergeCommands([stale], [newer])[0].status, "approved");
});

test("mergeEvents dedupes and sorts oldest first", () => {
  const e1 = ev("1", "2026-01-01T00:00:01Z");
  const e2 = ev("2", "2026-01-01T00:00:02Z");
  assert.deepEqual(mergeEvents([e2], [e1, e2]).map((e) => e.id), ["1", "2"]);
});

test("getMarker maps outcomes and ignores bad payloads", () => {
  const m = (eventType: AuditEvent["eventType"], payload: AuditEvent["payload"]) =>
    getMarker({ eventType, payload });
  assert.equal(m("validator_result", { outcome: "pass" }), "pass");
  assert.equal(m("validator_result", { outcome: "fail" }), "fail");
  assert.equal(m("critic_result", { outcome: "approve" }), "pass");
  assert.equal(m("critic_result", { outcome: "reject" }), "fail");
  assert.equal(m("critic_result", { outcome: "error" }), "error");
  assert.equal(m("critic_result", { outcome: "pass" }), undefined);
  assert.equal(m("critic_result", {}), undefined);
  assert.equal(m("critic_result", { outcome: 1 }), undefined);
  assert.equal(m("plan_generated", { outcome: "pass" }), undefined);
});
