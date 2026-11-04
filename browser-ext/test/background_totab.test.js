// Riko Helper — toTab() messaging rules (background.js), tested against a
// fake `browser` in a vm realm with a fake clock:
//   * "Could not establish connection. Receiving end does not exist." (the
//     Firefox no-listener error) and "Message manager closed" are pre-dispatch
//     errors -> safe to re-send, poll until the content script shows up;
//   * scripting.executeScript runs AT MOST ONCE per call, and only for the
//     no-receiving-end class;
//   * any other error (restricted page, no permission) fails fast, no retry,
//     no injection. Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const NO_END = "Could not establish connection. Receiving end does not exist.";

function fakeBackground({ mode, error = NO_END, reply = { ok: true, result: "pong" } }) {
  const state = { sends: 0, injects: 0, injected: false };
  let now = 0;  // fake clock: advances only when the script sleeps
  const noop = () => {};
  const browser = {
    runtime: {
      onMessage: { addListener: noop }, onStartup: { addListener: noop },
      onInstalled: { addListener: noop }, getManifest: () => ({ version: "0" }),
    },
    storage: {
      local: { get: async (d) => ({ ...d }), set: async () => {} },
      onChanged: { addListener: noop },
    },
    tabs: {
      query: async () => [],
      create: async () => ({ id: 99 }),
      hide: async () => {},
      get: async () => null,
      update: async () => {},
      onRemoved: { addListener: noop },
      sendMessage: async () => {
        state.sends += 1;
        if (mode === "late-register" && state.sends <= 3) throw new Error(error);
        if (mode === "forbidden") throw new Error("Cannot access a chrome:// URL");
        if (mode === "never-register" && !state.injected) throw new Error(error);
        if (mode === "inject-useless" || mode === "inject-throws") throw new Error(error);
        return reply;
      },
    },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    alarms: { create: noop, onAlarm: { addListener: noop } },
    scripting: {
      executeScript: async () => {
        state.injects += 1;
        if (mode === "inject-throws") throw new Error("Cannot access a chrome:// URL");
        state.injected = true;
      },
    },
  };
  const context = vm.createContext({
    browser,
    console,
    setTimeout: (fn) => setTimeout(fn, 0),   // real timer, fake clock
    clearTimeout: (t) => clearTimeout(t),
    Date: { now: () => now },
  });
  // background.js advances its poll deadlines via Date.now(); make sleep()
  // move the fake clock so the 2.5s budget burns instantly.
  vm.runInContext(readFileSync(path.join(here, "..", "background.js"), "utf8")
    .replace("const sleep = (ms) => new Promise((r) => setTimeout(r, ms));",
             "const sleep = (ms) => new Promise((r) => { globalThis.__advance(ms); setTimeout(r, 0); });"),
    context);
  context.__advance = (ms) => { now += ms; };
  return { context, state };
}

test("content script registers late: retried, succeeds, never injects", async () => {
  const { context, state } = fakeBackground({ mode: "late-register" });
  const out = await context.toTab(1, { cmd: "snapshot" });
  assert.deepEqual(out, { ok: true, result: "pong" });
  assert.equal(state.sends, 4);   // 1 fail + 2 polls fail + 1 success
  assert.equal(state.injects, 0, "a tab whose script is merely slow must not be injected");
});

test("page predates the extension: injects EXACTLY once, then succeeds", async () => {
  const { context, state } = fakeBackground({ mode: "never-register" });
  const out = await context.toTab(1, { cmd: "snapshot" });
  assert.deepEqual(out, { ok: true, result: "pong" });
  assert.equal(state.injects, 1, "at most one executeScript per toTab() call");
  assert.ok(state.sends > 3);
});

test("'Message manager closed' is retried without injecting", async () => {
  const { context, state } = fakeBackground({ mode: "late-register", error: "Message manager closed" });
  const out = await context.toTab(1, { cmd: "snapshot" });
  assert.deepEqual(out, { ok: true, result: "pong" });
  assert.equal(state.injects, 0);
});

test("non-receiving-end error: fails fast, no retry, no injection", async () => {
  const { context, state } = fakeBackground({ mode: "forbidden" });
  await assert.rejects(() => context.toTab(1, { cmd: "snapshot" }),
    /no access to that tab .*chrome:\/\//i);
  assert.equal(state.sends, 1);
  assert.equal(state.injects, 0);
});

test("injection that still can't reach a listener: one inject, then give up", async () => {
  const { context, state } = fakeBackground({ mode: "inject-useless" });
  await assert.rejects(() => context.toTab(1, { cmd: "snapshot" }),
    /no access to that tab/);
  assert.equal(state.injects, 1);
});

test("injection itself throws: no inject loop, error wrapped", async () => {
  const { context, state } = fakeBackground({ mode: "inject-throws" });
  await assert.rejects(() => context.toTab(1, { cmd: "snapshot" }),
    /no access to that tab/);
  assert.equal(state.injects, 1);
});
