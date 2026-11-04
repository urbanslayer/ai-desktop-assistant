// Riko Helper — link supervisor (background.js): the desktop probe gates the
// link tab, failures back off 1 -> 2 -> 4 min (cap), a manual close reopens
// only after a 60 s cooldown, and LINK_PREFIX / the error-page mark follow
// the configured port. Tested against a fake `browser` + fake `fetch` in a vm
// realm with a fake clock, like background_totab.test.js.
// Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));

function supervisor({ online = false, storage = {}, tabs = [] } = {}) {
  const state = {
    online, probes: [], created: [], hidden: [], reloaded: [], set: [],
    listeners: {}, alarms: [], tabSeq: 98, tabs, storage,
  };
  let now = 1_000_000;  // fake clock (non-zero so lastCloseTs=0 means "never")
  const noop = () => {};
  const browser = {
    runtime: {
      onMessage: { addListener: (f) => { state.listeners.message = f; } },
      onStartup: { addListener: noop }, onInstalled: { addListener: noop },
      getManifest: () => ({ version: "0" }),
    },
    storage: {
      local: {
        get: async (d) => ({ ...d, ...state.storage }),
        set: async (o) => { state.set.push(o); },
      },
      onChanged: { addListener: (f) => { state.listeners.changed = f; } },
    },
    tabs: {
      query: async () => state.tabs,
      create: async (opts) => {
        const tab = { id: ++state.tabSeq, ...opts };
        state.created.push(tab);
        state.tabs.push(tab);   // like the real browser: the tab now exists
        return tab;
      },
      hide: async (id) => { state.hidden.push(id); },
      reload: async (id) => { state.reloaded.push(id); },
      get: async () => null, update: async () => {},
      onRemoved: { addListener: (f) => { state.listeners.removed = f; } },
      sendMessage: async () => ({ ok: true }),
    },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    scripting: { executeScript: async () => {} },
    alarms: {
      create: (name, spec) => { state.alarms.push([name, spec]); },
      onAlarm: { addListener: (f) => { state.listeners.alarm = f; } },
    },
  };
  const fetch = async (url) => {
    state.probes.push(url);
    if (!state.online) throw new TypeError("Connection refused");  // desktop down
    return { ok: true, json: async () => ({ ok: true, name: "riko-desktop", version: "1" }) };
  };
  class AbortController { constructor() { this.signal = { aborted: false }; } abort() {} }
  const context = vm.createContext({
    browser, console, fetch, AbortController,
    setTimeout: (fn) => setTimeout(fn, 0),   // real timer, fake clock
    clearTimeout: (t) => clearTimeout(t),
    Date: { now: () => now },
    TypeError,
  });
  vm.runInContext(readFileSync(path.join(here, "..", "background.js"), "utf8")
    .replace("const sleep = (ms) => new Promise((r) => setTimeout(r, ms));",
             "const sleep = (ms) => new Promise((r) => { globalThis.__advance(ms); setTimeout(r, 0); });"),
    context);
  context.__advance = (ms) => { now += ms; };
  const tick = async (n = 8) => {
    for (let i = 0; i < n; i += 1) await new Promise((r) => setTimeout(r, 0));
  };
  return { context, state, tick, setOnline: (v) => { state.online = v; } };
}

test("desktop offline: no tab opens, backoff is 1 -> 2 -> 4 -> 4 min", async () => {
  const { context, state, tick } = supervisor({ online: false });
  await tick();  // the load-time ensureLinkTab runs
  assert.equal(state.probes.length, 1, "the load-time supervisor probes once");
  assert.equal(state.created.length, 0, "no tab when /hello doesn't answer");
  assert.equal(state.set.at(-1).desktopOnline, false);

  // failure #1 arms nextProbeAt = 0 + 60 s
  context.__advance(30_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 1, "gated by backoff before 1 min");
  context.__advance(31_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 2, "retry at 1 min");
  assert.equal(state.created.length, 0);

  context.__advance(119_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 2, "failure #2 waits 2 min, not less");
  context.__advance(2_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 3, "retry at 2 min");

  context.__advance(239_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 3, "failure #3 waits 4 min");
  context.__advance(2_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 4, "retry at 4 min");

  context.__advance(239_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 4, "backoff caps at 4 min");
  context.__advance(2_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.probes.length, 5, "and keeps retrying on that cap");
});

test("desktop online: the link tab opens on the configured port, hidden", async () => {
  const { context, state, tick, setOnline } = supervisor({ online: false });
  await tick();
  assert.equal(state.created.length, 0);
  setOnline(true);
  context.__advance(61_000);  // past the backoff armed by the failed probe
  await context.ensureLinkTab(); await tick();
  assert.equal(state.created.length, 1);
  assert.equal(state.created[0].url, "http://127.0.0.1:8766/link");
  assert.equal(state.created[0].active, false);
  assert.deepEqual(state.hidden, [state.created[0].id], "the tab is hidden");
  assert.equal(state.set.at(-1).desktopOnline, true);
});

test("port setting: link prefix + error-page mark follow 8767", async () => {
  const { context, state, tick } = supervisor({ online: true, storage: { port: 8767 } });
  await tick();
  assert.equal(state.created[0].url, "http://127.0.0.1:8767/link");
  assert.equal(context.isLinkTab({ url: "http://127.0.0.1:8767/link" }), true);
  assert.equal(
    context.isLinkTab({ url: "about:neterror?e=connectionFailure&u=http%3A%2F%2F127.0.0.1%3A8767%2Flink" }),
    true, "the error page for the CURRENT port still counts as the link tab");
  assert.equal(context.isLinkTab({ url: "http://127.0.0.1:8766/link" }), false,
    "a tab on the OLD port is no longer the link tab");
  assert.equal(
    context.isLinkTab({ url: "about:neterror?e=connectionFailure&u=http%3A%2F%2F127.0.0.1%3A8766%2Flink" }),
    false);

  // the tab already exists on 8767: the supervisor keeps it, opens nothing
  state.tabs = [{ id: 5, url: "http://127.0.0.1:8767/link" }];
  state.created.length = 0;
  await context.ensureLinkTab(); await tick();
  assert.equal(state.created.length, 0);

  // changing the port in storage resets the supervisor and re-probes
  state.storage = { port: 8768 };
  state.tabs = [];
  state.listeners.changed({ port: { newValue: 8768 } });
  await tick();
  assert.equal(state.created.at(-1).url, "http://127.0.0.1:8768/link");
});

test("manual close: 60 s cooldown, lastCloseTs stored, then reopens", async () => {
  const { context, state, tick } = supervisor({ online: true });
  await tick();
  const linkTab = state.created[0];
  assert.ok(linkTab, "tab opened while the desktop is online");

  state.tabs = [];                               // the tab is gone...
  state.listeners.removed(linkTab.id);           // ...the user closed it
  await tick();                                  // the 1.2 s ensure attempt
  assert.equal(state.created.length, 1, "cooldown: not reopened instantly");
  assert.equal(state.set.at(-1).lastCloseTs, 1_000_000, "close time stored");

  context.__advance(61_000);
  await context.ensureLinkTab(); await tick();
  assert.equal(state.created.length, 2, "reopened after the 60 s cooldown");

  // unrelated tabs closing never burn a reopen
  const closed = state.created[1];
  state.listeners.removed(closed.id + 500);
  await tick();
  assert.equal(state.created.length, 2);
});

test("autoReopen off: no probe, no tab; popup Connect forces one anyway", async () => {
  const { context, state, tick } = supervisor({ online: true, storage: { autoReopen: false } });
  await tick();
  assert.equal(state.probes.length, 0, "the supervisor stays quiet while the toggle is off");
  assert.equal(state.created.length, 0);

  const answer = await state.listeners.message({ cmd: "connect" });
  await tick();
  assert.equal(answer.online, true);
  // the handler probes once for the answer, the forced ensure re-checks: 2
  assert.equal(state.probes.length, 2, "Connect probes immediately, twice even");
  assert.equal(state.created.length, 1, "Connect opens the tab despite the toggle");
});

test("riko-status alarm records desktopOnline; riko-link alarm honours backoff", async () => {
  const { context, state, tick, setOnline } = supervisor({ online: false });
  await tick();
  assert.deepEqual(state.alarms.map(([n]) => n).sort(), ["riko-link", "riko-status"]);

  await state.listeners.alarm({ name: "riko-status" }); await tick();
  assert.equal(state.set.at(-1).desktopOnline, false);

  context.__advance(30_000);
  const probesBefore = state.probes.length;   // the status alarm re-probes; that's fine
  await state.listeners.alarm({ name: "riko-link" }); await tick();
  assert.equal(state.probes.length, probesBefore, "riko-link stays gated by the probe backoff");

  setOnline(true);
  await state.listeners.alarm({ name: "riko-status" }); await tick();
  // (probe #2 was gated for riko-link; riko-status always re-probes)
  assert.equal(state.set.at(-1).desktopOnline, true);
});
