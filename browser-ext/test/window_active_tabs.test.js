// Riko Helper — activeTabs (multi-window survey, v0.1.3). background.js runs
// in a vm realm against a fake `browser` with a windows API, so the pure
// ordering/tracking rules are testable without Firefox:
//   * focus order comes from windows.onFocusChanged (most recent first),
//     mirrored into storage.session; a closed window leaves the order;
//   * onFocusChanged(WINDOW_ID_NONE) => focusedWindowId null, no window
//     focused, order preserved (e.g. all windows minimized);
//   * a fresh worker with nothing tracked adopts windows.getLastFocused as
//     the last-focused window (leads the list with a fresh timestamp);
//   * window state (normal|maximized|minimized|fullscreen) passes through;
//   * private windows only appear when shareIncognito is on;
//   * the link tab is never counted and never reported: when it is a window's
//     active tab, activeTab is null and title "";
//   * activeTab keeps its 0.1.2 single-window behaviour unchanged;
//   * the toolbar kill switch refuses activeTabs too.
// Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const PORT = 8766;
const LINK_URL = `http://127.0.0.1:${PORT}/link`;

const tab = (o) => ({
  active: false, pinned: false, audible: false, incognito: false,
  windowType: "normal", index: 0, windowId: 1,
  title: "untitled", url: "https://example.com/", lastAccessed: 1700000000000,
  ...o,
});

const win = (o) => ({ id: 1, incognito: false, state: "normal", type: "normal", ...o });

// windows: array of {id, state?, incognito?, tabs: [tab,...]}
function fakeBackground({ stored = {}, windows = [], focusedWindowId = 1,
                          windowsDown = false, session = null } = {}) {
  const noop = () => {};
  const allTabs = windows.flatMap((w) => (w.tabs || []).map((t) => ({ ...t, windowId: w.id })));
  const listeners = { focus: [], removed: [] };
  const sessionStore = { ...(session || {}) };
  const sessionWrites = [];
  const browser = {
    runtime: {
      onMessage: { addListener: noop }, onStartup: { addListener: noop },
      onInstalled: { addListener: noop }, getManifest: () => ({ version: "0" }),
    },
    storage: {
      local: { get: async (d) => ({ ...d, ...stored }), set: async () => {} },
      session: {
        get: async (keys) => {
          const out = {};
          for (const k of [].concat(keys)) if (k in sessionStore) out[k] = sessionStore[k];
          return out;
        },
        set: async (obj) => { sessionWrites.push(obj); Object.assign(sessionStore, obj); },
      },
      onChanged: { addListener: noop },
    },
    tabs: {
      query: async (opts = {}) => allTabs.filter((t) => {
        if (opts.windowType && t.windowType !== opts.windowType) return false;
        if (opts.windowId != null && t.windowId !== opts.windowId) return false;
        if (opts.active && !t.active) return false;
        if (opts.currentWindow && t.windowId !== focusedWindowId) return false;
        return true;
      }),
      get: async (id) => allTabs.find((t) => t.id === id) || null,
      create: async ({ url }) => ({ id: 900, url, windowId: focusedWindowId, index: 99 }),
      hide: async () => {}, reload: async () => {}, update: async () => {},
      remove: async () => {}, goBack: async () => {},
      sendMessage: async () => { throw new Error("no content script in this fake"); },
      onRemoved: { addListener: noop },
    },
    windows: {
      getLastFocused: async () => {
        if (windowsDown) throw new Error("no window");
        return { id: focusedWindowId };
      },
      getAll: async (opts = {}) => {
        if (windowsDown) throw new Error("no window");
        return windows
          .filter((w) => !opts.windowTypes || opts.windowTypes.includes(w.type))
          .map((w) => ({
            id: w.id, incognito: !!w.incognito, state: w.state || "normal",
            type: w.type || "normal",
            tabs: opts.populate ? (w.tabs || []).map((t) => ({ ...t, windowId: w.id })) : undefined,
          }))
          .sort((a, b) => a.id - b.id);
      },
      getWindow: async (id) => windows.find((w) => w.id === id) || null,
      onFocusChanged: { addListener: (fn) => listeners.focus.push(fn) },
      onRemoved: { addListener: (fn) => listeners.removed.push(fn) },
      WINDOW_ID_NONE: -1,
    },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    alarms: { create: noop, onAlarm: { addListener: noop } },
    scripting: { executeScript: async () => {} },
  };
  const context = vm.createContext({ browser, console, setTimeout, clearTimeout, Date });
  vm.runInContext(readFileSync(path.join(here, "..", "background.js"), "utf8"), context);
  context.__focus = (id) => listeners.focus.forEach((fn) => fn(id));
  context.__remove = (id) => listeners.removed.forEach((fn) => fn(id));
  context.__session = sessionStore;
  context.__sessionWrites = sessionWrites;
  return context;
}

// microtask drain: onFocusChanged handlers run via loadFocusOrder().then(...)
const settle = () => new Promise((r) => setImmediate(r));

const SHAPE = ["id", "windowId", "title", "url", "active", "pinned", "audible",
               "incognito", "lastAccessed"];
const WSHAPE = ["windowId", "focused", "state", "title", "tabCount",
                "lastFocusedAt", "activeTab"];

function threeWindows() {
  return [
    win({ id: 1, tabs: [
      tab({ id: 11, active: true, title: "Wiki", url: "https://wiki.example.org/" }),
      tab({ id: 12, index: 1, title: "Docs", url: "https://docs.example.org/" }),
    ] }),
    win({ id: 2, state: "maximized", tabs: [
      tab({ id: 21, active: true, windowId: 2, title: "Mail", url: "https://mail.example.org/" }),
      tab({ id: 22, windowId: 2, index: 1, title: "Riko link", url: LINK_URL }),
    ] }),
    win({ id: 3, tabs: [
      tab({ id: 31, active: true, windowId: 3, title: "News", url: "https://news.example.org/" }),
    ] }),
  ];
}

test("activeTabs: focus order decides the order, most recent first", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows() });
  ctx.__focus(3); await settle();
  ctx.__focus(1); await settle();
  ctx.__focus(2); await settle();
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.ok, true);
  const { focusedWindowId, windows } = out.result;
  assert.equal(focusedWindowId, 2);
  assert.deepEqual(windows.map((w) => w.windowId), [2, 1, 3]);
  assert.deepEqual(windows.map((w) => w.focused), [true, false, false]);
  assert.equal(out.result.windows[0].activeTab.id, 21);
  assert.equal(windows[0].title, "Mail");
  assert.equal(windows[0].state, "maximized");
  assert.equal(windows[0].tabCount, 1);        // link tab never counted
  assert.equal(windows[1].tabCount, 2);
  // lastFocusedAt strictly descending along the order, all recorded
  const times = windows.map((w) => w.lastFocusedAt);
  assert.ok(times.every((t) => typeof t === "number"));
  assert.ok(times[0] >= times[1] && times[1] >= times[2], `times not ordered: ${times}`);
  for (const w of windows) {
    assert.deepEqual(Object.keys(w).sort(), [...WSHAPE].sort(), `bad shape: ${JSON.stringify(w)}`);
    assert.deepEqual(Object.keys(w.activeTab).sort(), [...SHAPE].sort());
  }
});

test("activeTabs: re-focusing the same window moves it up, keeps one slot", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows() });
  ctx.__focus(1); await settle();
  ctx.__focus(2); await settle();
  ctx.__focus(1); ctx.__focus(1); await settle();  // double event = one entry
  const out = await ctx.handleCommand("activeTabs", {});
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [1, 2, 3]);
  assert.equal(out.result.focusedWindowId, 1);
});

test("activeTabs: WINDOW_ID_NONE clears focused but keeps the order", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows() });
  ctx.__focus(2); await settle();
  ctx.__focus(1); await settle();
  ctx.__focus(-1); await settle();               // everything minimized
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.result.focusedWindowId, null);
  assert.equal(out.result.windows.some((w) => w.focused), false);
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [1, 2, 3]);
  assert.ok(typeof out.result.windows[0].lastFocusedAt === "number");
});

test("activeTabs: fresh worker adopts getLastFocused, leads with a fresh time", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows(),
                               focusedWindowId: 3 });
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.result.focusedWindowId, 3);
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [3, 1, 2]);
  assert.equal(out.result.windows[0].focused, true);
  assert.equal(typeof out.result.windows[0].lastFocusedAt, "number");
  assert.equal(out.result.windows[1].focused, false);
  assert.equal(out.result.windows[1].lastFocusedAt, null);  // never observed
});

test("activeTabs: persisted storage.session order survives a worker restart", async () => {
  // pre-seeded session store: the previous worker saw window 2 focused last
  const session = { focusOrder: [2, 3, 1], focusTimes: { 1: 100, 2: 300, 3: 200 },
                    focusedWindowId: 2 };
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows(),
                               focusedWindowId: 1, session });
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.result.focusedWindowId, 2);   // not getLastFocused (1)
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [2, 3, 1]);
  assert.deepEqual(out.result.windows.map((w) => w.lastFocusedAt), [300, 200, 100]);
});

test("activeTabs: focus events persist to storage.session", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows() });
  ctx.__focus(2); await settle();
  assert.ok(ctx.__sessionWrites.length >= 1, "nothing persisted to storage.session");
  const last = ctx.__sessionWrites[ctx.__sessionWrites.length - 1];
  assert.deepEqual(last.focusOrder[0], 2);
  assert.equal(last.focusedWindowId, 2);
  assert.equal(typeof last.focusTimes[2], "number");
});

test("activeTabs: closed windows drop out of the order (event + sweep)", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows() });
  ctx.__focus(3); ctx.__focus(2); ctx.__focus(1); await settle();
  ctx.__remove(2); await settle();               // onRemoved drops it
  const live = threeWindows().filter((w) => w.id !== 2);
  const ctx2 = fakeBackground({ stored: { port: PORT }, windows: live,
                                session: { focusOrder: [1, 2, 3],
                                           focusTimes: { 1: 3, 2: 2, 3: 1 },
                                           focusedWindowId: 1 } });
  // even without the onRemoved event, doActiveTabs prunes non-live windows
  const out = await ctx2.handleCommand("activeTabs", {});
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [1, 3]);
  const out2 = await ctx.handleCommand("activeTabs", {});
  // window 2 is alive again in this ctx's view: untracked ids sort last
  assert.deepEqual(out2.result.windows.map((w) => w.windowId), [1, 3, 2]);
  assert.equal(out2.result.focusedWindowId, 1);
});

test("activeTabs: minimized windows keep their place and report the state", async () => {
  const wins = threeWindows();
  wins[1].state = "minimized";                   // window 2 got minimized after focus
  const ctx = fakeBackground({ stored: { port: PORT }, windows: wins });
  ctx.__focus(2); await settle();                // focus BEFORE it was minimized
  ctx.__focus(1); await settle();
  const out = await ctx.handleCommand("activeTabs", {});
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [1, 2, 3]);
  const min = out.result.windows.find((w) => w.windowId === 2);
  assert.equal(min.state, "minimized");
  assert.equal(min.focused, false);
  assert.equal(typeof min.lastFocusedAt, "number", "minimize must not erase history");
});

test("activeTabs: private windows excluded with sharing off, included with it on", async () => {
  const wins = threeWindows().concat(win({ id: 9, incognito: true, type: "normal", tabs: [
    tab({ id: 91, active: true, incognito: true, title: "Private bank",
          url: "https://bank.example/", windowId: 9 }),
  ] }));
  const off = fakeBackground({ stored: { port: PORT }, windows: wins });
  off.__focus(9); await settle();
  const offOut = await off.handleCommand("activeTabs", {});
  assert.deepEqual(offOut.result.windows.map((w) => w.windowId), [1, 2, 3]);
  assert.equal(offOut.result.focusedWindowId, null);  // private lead not reportable
  assert.equal(offOut.result.windows.some((w) => w.focused), false);

  const on = fakeBackground({ stored: { port: PORT, shareIncognito: true }, windows: wins });
  on.__focus(9); await settle();
  const out = await on.handleCommand("activeTabs", {});
  assert.deepEqual(out.result.windows.map((w) => w.windowId), [9, 1, 2, 3]);
  assert.equal(out.result.focusedWindowId, 9);
  assert.equal(out.result.windows[0].focused, true);
  assert.equal(out.result.windows[0].activeTab.incognito, true);
});

test("activeTabs: link tab as active tab -> activeTab null, title '', link never counted", async () => {
  const wins = threeWindows();
  wins[2].tabs = [                                // window 3: link tab is the active tab
    tab({ id: 31, windowId: 3, title: "News", url: "https://news.example.org/" }),
    tab({ id: 32, windowId: 3, index: 1, active: true, title: "Riko link", url: LINK_URL }),
  ];
  const ctx = fakeBackground({ stored: { port: PORT }, windows: wins });
  ctx.__focus(3); await settle();
  const out = await ctx.handleCommand("activeTabs", {});
  const w3 = out.result.windows.find((w) => w.windowId === 3);
  assert.equal(w3.focused, true);                 // focused is about the window
  assert.equal(w3.activeTab, null);               // but the link tab is never reported
  assert.equal(w3.title, "");
  assert.equal(w3.tabCount, 1);                   // the News tab, link excluded
});

test("activeTabs: link tab as only tab -> tabCount 0, other windows unaffected", async () => {
  const wins = [win({ id: 4, tabs: [
    tab({ id: 41, windowId: 4, active: true, title: "Riko link", url: LINK_URL }),
  ] })];
  const ctx = fakeBackground({ stored: { port: PORT }, windows: wins, focusedWindowId: 4 });
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.result.windows.length, 1);
  const w4 = out.result.windows[0];
  // window 4 is what getLastFocused reports, so the fresh worker adopts it
  assert.equal(typeof w4.lastFocusedAt, "number");
  w4.lastFocusedAt = null;
  assert.equal(JSON.stringify(w4), JSON.stringify({
    windowId: 4, focused: true, state: "normal", title: "", tabCount: 0,
    lastFocusedAt: null, activeTab: null,
  }));
});

test("activeTabs: window with no tabs answers tabCount 0 + null activeTab", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: [win({ id: 5, tabs: [] })] });
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.result.windows[0].tabCount, 0);
  assert.equal(out.result.windows[0].activeTab, null);
});

test("activeTabs: titles capped at 200 chars", async () => {
  const wins = threeWindows();
  wins[0].tabs[0].title = "x".repeat(250);
  const ctx = fakeBackground({ stored: { port: PORT }, windows: wins });
  const out = await ctx.handleCommand("activeTabs", {});
  const w1 = out.result.windows.find((w) => w.windowId === 1);
  assert.equal(w1.title.length, 200);
  assert.equal(w1.activeTab.title.length, 200);
});

test("activeTab (0.1.2) unchanged: last-focused window only, null-safe", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: threeWindows(),
                               focusedWindowId: 2 });
  const out = await ctx.handleCommand("activeTab", {});
  assert.equal(out.result.id, 21);                // window 2's active tab, not 22
  assert.deepEqual(Object.keys(out.result).sort(), [...SHAPE].sort());
  const down = fakeBackground({ stored: { port: PORT }, windows: [], windowsDown: true });
  assert.equal((await down.handleCommand("activeTab", {})).result, null);
});

test("activeTabs: null-safe when no window answers, result still ok", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, windows: [], windowsDown: true });
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.ok, true);
  // vm-realm objects are structurally equal but not prototype-equal: compare plain JSON
  assert.equal(JSON.stringify(out.result), '{"focusedWindowId":null,"windows":[]}');
});

test("paused kill switch refuses activeTabs too", async () => {
  const ctx = fakeBackground({ stored: { port: PORT, paused: true }, windows: threeWindows() });
  const out = await ctx.handleCommand("activeTabs", {});
  assert.equal(out.ok, false);
  assert.match(out.error, /paused/);
});
