// Riko Helper — content.js must be idempotent. The background's toTab() may
// scripting.executeScript [lib/risk.js, lib/snapshot.js, content.js] onto a
// page whose declared content script is already present (Firefox 157 lets the
// registration race). Without the window guard, every re-injection stacks a
// second runtime.onMessage listener -> one browser_click becomes two clicks,
// typed text doubles, forms submit twice. Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = (rel) => readFileSync(path.join(here, "..", rel), "utf8");

// ── a minimal fake page + fake `browser`, run in a vm realm ──────────────
function fakePage() {
  const state = { clicks: 0, focus: 0, inputEvents: 0, listeners: [] };

  const button = {
    tagName: "BUTTON",
    labels: null,
    innerText: "Say hi",
    value: "",
    name: "",
    form: null,
    disabled: false,
    getAttribute: (a) => (a === "data-riko-id" ? "1" : null),
    setAttribute: () => {},
    closest: () => null,
    getClientRects: () => [{}],
    focus: () => { state.focus += 1; },
    click: () => { state.clicks += 1; },
    dispatchEvent: () => { state.inputEvents += 1; return true; },
  };
  const input = {
    tagName: "INPUT",
    type: "text",
    labels: null,
    innerText: "",
    value: "",
    name: "q",
    form: null,
    getAttribute: (a) => (a === "data-riko-id" ? "2" : null),
    setAttribute: () => {},
    closest: () => null,
    getClientRects: () => [{}],
    focus: () => { state.focus += 1; },
    click: () => { state.clicks += 1; },
    dispatchEvent: () => { state.inputEvents += 1; return true; },
  };

  const context = vm.createContext({
    window: {},
    document: {
      title: "fake page",
      body: { innerText: "hello world", scrollHeight: 1000, appendChild() {} },
      documentElement: { appendChild() {} },
      getElementById: () => null,
      createElement: () => ({ setAttribute() {} }),
      // snapshot walks with querySelectorAll; act resolves by data-riko-id
      querySelectorAll: () => [button, input],
      querySelector: (sel) =>
        sel.includes('data-riko-id="1"') ? button
        : sel.includes('data-riko-id="2"') ? input : null,
    },
    location: { href: "http://test.local/page", host: "test.local" },
    browser: { runtime: { onMessage: { addListener: (fn) => state.listeners.push(fn) } } },
    getComputedStyle: () => ({ visibility: "visible", display: "block" }),
    CSS: { escape: (s) => String(s) },
    innerHeight: 800,
    scrollTo() {}, scrollBy() {},
    setTimeout: (fn) => setTimeout(fn, 0),
    clearTimeout: (t) => clearTimeout(t),
    HTMLInputElement: function () {},
    HTMLTextAreaElement: function () {},
    HTMLSelectElement: function () {},
    Event: function (type) { this.type = type; },
    KeyboardEvent: function (type) { this.type = type; },
  });
  // setNativeValue reads the "value" setter off the prototype objects
  for (const proto of ["HTMLInputElement", "HTMLTextAreaElement", "HTMLSelectElement"]) {
    Object.defineProperty(context[proto].prototype, "value", {
      set() {}, get() { return ""; }, configurable: true,
    });
  }
  const inject = (rel) => vm.runInContext(src(rel), context);
  const injectAll = () => { inject("lib/risk.js"); inject("lib/snapshot.js"); inject("content.js"); };
  return { context, state, inject, injectAll };
}

test("double injection => exactly one onMessage listener", () => {
  const { state, injectAll } = fakePage();
  injectAll();
  injectAll();               // the executeScript fallback racing the declared script
  assert.equal(state.listeners.length, 1,
    `expected 1 listener after double injection, got ${state.listeners.length}`);
});

test("one 'act' click after double injection clicks the element once", async () => {
  const { context, state, injectAll } = fakePage();
  injectAll();
  injectAll();
  assert.equal(state.listeners.length, 1);
  const out = await state.listeners[0]({ cmd: "act", kind: "click", n: 1, id: "1" });
  assert.equal(out.ok, true, out.error);
  assert.match(out.result, /clicked Say hi on test\.local/);
  assert.equal(state.clicks, 1, "one command must produce exactly one el.click()");
  assert.equal(state.focus, 1);
});

test("one 'act' type after double injection fires input/change once", async () => {
  const { state, injectAll } = fakePage();
  injectAll();
  injectAll();
  const out = await state.listeners[0]({ cmd: "act", kind: "type", n: 2, id: "2", text: "hi" });
  assert.equal(out.ok, true, out.error);
  assert.equal(state.inputEvents, 2, "one input + one change event, not doubled");
});

test("data-riko-id numbering is stable across re-injection", async () => {
  const { state, injectAll } = fakePage();
  injectAll();
  const s1 = await state.listeners[0]({ cmd: "snapshot" });
  injectAll();  // background injects again mid-session
  const s2 = await state.listeners[0]({ cmd: "snapshot" });
  assert.deepEqual(s1.elements.map((e) => e.id), s2.elements.map((e) => e.id));
  assert.deepEqual(s1.elements.map((e) => e.n), s2.elements.map((e) => e.n));
});

test("re-running lib globals keeps the same object (idempotent libs)", () => {
  const { context, inject } = fakePage();
  inject("lib/risk.js");
  inject("lib/snapshot.js");
  const risk = context.RikoRisk, snap = context.RikoSnapshot;
  inject("lib/risk.js");
  inject("lib/snapshot.js");
  assert.equal(context.RikoRisk, risk, "RikoRisk must not be rebuilt");
  assert.equal(context.RikoSnapshot, snap, "RikoSnapshot must not be rebuilt");
});
