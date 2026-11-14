import { test } from "node:test";
import assert from "node:assert/strict";
import { caseHeaderLayout } from "./case-header.ts";
const card = { top: -200, bottom: 800, left: 400, width: 600 };
test("compact header stays hidden until the entire title block clears the pane", () => {
  assert.equal(caseHeaderLayout(1, card, 0, 900, 64), null);
  assert.deepEqual(caseHeaderLayout(0, card, 0, 900, 64), { top: 0, left: 400, width: 600, clip: 264 });
});
test("mobile app bar and inset preview panes are accounted for", () => {
  assert.equal(caseHeaderLayout(181, card, 180, 844, 80), null);
  assert.deepEqual(caseHeaderLayout(180, card, 180, 844, 80), { top: 180, left: 400, width: 600, clip: 460 });
  assert.equal(caseHeaderLayout(50, card, 50, 900, 64)?.clip, 314);
});
test("header leaves when its case ends and never appears in a shorter pane", () => {
  assert.equal(caseHeaderLayout(-10, { ...card, bottom: 64 }, 0, 900, 64), null);
  assert.equal(caseHeaderLayout(-10, card, 0, 50, 64), null);
  assert.equal(caseHeaderLayout(NaN, card, 0, 900, 64), null);
});
