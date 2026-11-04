// Riko Helper — snapshot numbering / serialisation rules.
// Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const RikoSnapshot = require("../lib/snapshot.js");
const { buildSnapshot, isInteractive } = RikoSnapshot;

test("numbering is 1-based, dense, and skips hidden elements", () => {
  const snap = buildSnapshot({
    title: "T", url: "https://a.dev", text: "hello",
    elements: [
      { id: "1", tag: "A", text: "one" },
      { id: "2", tag: "DIV", text: "ghost", hidden: true },
      { id: "3", tag: "BUTTON", text: "two" },
    ],
  });
  assert.deepEqual(snap.elements.map((e) => e.n), [1, 2]);
  assert.deepEqual(snap.elements.map((e) => e.text), ["one", "two"]);
  assert.equal(snap.elements[1].id, "3");  // content-script handle survives renumbering
});

test("password values are never serialised", () => {
  const snap = buildSnapshot({
    title: "", url: "", text: "",
    elements: [
      { id: "1", tag: "INPUT", inputType: "password", value: "hunter2" },
      { id: "2", tag: "INPUT", inputType: "text", value: "visible" },
    ],
  });
  const json = JSON.stringify(snap);
  assert.ok(!json.includes("hunter2"), "the real password leaked into the snapshot");
  assert.equal(snap.elements[0].value, "••••••");
  assert.equal(snap.elements[1].value, "visible");
});

test("empty password fields stay empty (no fake bullets)", () => {
  const snap = buildSnapshot({ elements: [{ id: "1", tag: "INPUT", inputType: "password", value: "" }] });
  assert.equal(snap.elements[0].value, "");
});

test("duplicate elements collapse", () => {
  const snap = buildSnapshot({
    elements: [{ id: "7", tag: "A", text: "x" }, { id: "7", tag: "A", text: "x" }],
  });
  assert.equal(snap.elements.length, 1);
});

test("text is clamped and truncation is flagged", () => {
  const snap = buildSnapshot({ text: "z".repeat(9000), elements: [] });
  assert.ok(snap.text.length <= RikoSnapshot.MAX_TEXT);
  assert.equal(snap.truncated, true);
});

test("element cap holds and labels are clamped", () => {
  const many = Array.from({ length: 200 }, (_, i) => ({ id: String(i), tag: "A", text: "l".repeat(500) }));
  const snap = buildSnapshot({ elements: many });
  assert.equal(snap.elements.length, RikoSnapshot.MAX_ELEMENTS);
  assert.ok(snap.elements[0].text.length <= 160);
  assert.equal(snap.truncated, true);
});

test("junk input yields a safe empty snapshot", () => {
  assert.deepEqual(buildSnapshot({}).elements, []);
  assert.equal(buildSnapshot(null).text, "");
  const weird = buildSnapshot({ elements: [null, 42, { tag: "BUTTON", text: 7 }] });
  assert.equal(weird.elements.length, 1);
});

test("isInteractive knows the usual suspects", () => {
  assert.ok(isInteractive("A", ""));
  assert.ok(isInteractive("button", ""));
  assert.ok(isInteractive("DIV", "button"));
  assert.ok(!isInteractive("DIV", ""));
  assert.ok(!isInteractive("P", "presentation"));
});
