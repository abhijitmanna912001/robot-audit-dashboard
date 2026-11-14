import { test } from "node:test";
import assert from "node:assert/strict";
import { formatLocal, formatRelative } from "./format.ts";

const opts = { timeZone: "UTC", locale: "en-GB" };
const now = new Date("2026-11-14T12:00:00Z");

test("same day: time only; other day: date and time", () => {
  assert.equal(formatLocal("2026-11-14T09:15:02Z", now, { ...opts, withDate: true }), "09:15:02");
  assert.equal(formatLocal("2026-11-13T09:15:02Z", now, { ...opts, withDate: true }), "13 Nov, 09:15:02");
  assert.equal(formatLocal("2025-11-13T09:15:02Z", now, { ...opts, withDate: true }), "13 Nov 2025, 09:15:02");
});

test("without withDate it is always the time", () => {
  assert.equal(formatLocal("2026-11-13T09:15:02Z", now, opts), "09:15:02");
});

test("the day is judged in the viewer's time zone", () => {
  // 23:30 UTC on the 13th is already the 14th in Tokyo.
  assert.equal(formatLocal("2026-11-13T23:30:00Z", now, { timeZone: "Asia/Tokyo", locale: "en-GB", withDate: true }), "08:30:00");
  assert.equal(formatLocal("2026-11-13T23:30:00Z", now, { ...opts, withDate: true }), "13 Nov, 23:30:00");
});

test("midnight is 00, not 24", () => {
  assert.equal(formatLocal("2026-11-14T00:00:05Z", now, opts), "00:00:05");
});

test("bad input", () => {
  assert.equal(formatLocal("nope", now, opts), undefined);
  assert.equal(formatLocal("", now, opts), undefined);
  assert.equal(formatLocal("2026-11-13T09:15:02Z", new Date(NaN), { ...opts, withDate: true }), "09:15:02");
});

test("relative age keeps seconds, minutes, hours and days distinct", () => {
  assert.equal(formatRelative("2026-11-14T12:00:00Z", now), "just now");
  assert.equal(formatRelative("2026-11-14T11:59:15Z", now), "45 s ago");
  assert.equal(formatRelative("2026-11-14T11:59:00Z", now), "1 min ago");
  assert.equal(formatRelative("2026-11-14T11:00:00Z", now), "1 h ago");
  assert.equal(formatRelative("2026-11-14T09:00:00Z", now), "3 h ago");
  assert.equal(formatRelative("2026-11-12T12:00:00Z", now), "2 d ago");
});
test("relative age handles future timestamps and invalid dates", () => {
  assert.equal(formatRelative("2026-11-14T15:00:00Z", now), "in 3 h");
  assert.equal(formatRelative("invalid", now), undefined);
  assert.equal(formatRelative("2026-11-14T09:00:00Z", new Date(NaN)), undefined);
});
