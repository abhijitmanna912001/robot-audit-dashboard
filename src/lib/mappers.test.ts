import { test } from "node:test";
import assert from "node:assert/strict";
import { toAuditEvent, toCommand } from "./mappers.ts";

const cmdRow = {
  id: "c1", text: "go", status: "received", plan_id: null, claimed_by: null,
  claimed_at: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:00Z",
};
const evRow = {
  id: "e1", command_id: "c1", event_type: "validator_result", message: "m",
  payload: { outcome: "pass" }, created_at: "2026-11-14T10:00:01Z",
};

test("toCommand maps snake_case and keeps nulls", () => {
  assert.deepEqual(toCommand(cmdRow), {
    id: "c1", text: "go", status: "received", planId: null, claimedBy: null,
    claimedAt: null, createdAt: cmdRow.created_at, updatedAt: cmdRow.updated_at,
  });
  assert.equal(toCommand({ ...cmdRow, plan_id: "p1" })?.planId, "p1");
});

test("toCommand rejects unknown status and non-objects", () => {
  assert.equal(toCommand({ ...cmdRow, status: "planning" }), null);
  assert.equal(toCommand(null), null);
  assert.equal(toCommand({ ...cmdRow, id: undefined }), null);
});

test("toAuditEvent maps and coerces a bad payload to {}", () => {
  assert.equal(toAuditEvent(evRow)?.commandId, "c1");
  assert.deepEqual(toAuditEvent({ ...evRow, payload: "x" })?.payload, {});
  assert.deepEqual(toAuditEvent({ ...evRow, payload: [1] })?.payload, {});
  assert.equal(toAuditEvent({ ...evRow, event_type: "rejected" }), null);
});
