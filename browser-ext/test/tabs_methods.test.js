// Riko Helper — listTabs / activeTab (desktop awareness, docs/DESKTOP_SENSE.md
// "Browser tabs"). background.js runs in a vm realm against a fake `browser`
// so the pure filtering rules are testable without Firefox:
//   * incognito tabs dropped unless storage.shareIncognito is on;
//   * the link tab (http://127.0.0.1:<port>/link and its error-page form)
//     never appears;
//   * internal pages (about:, moz-extension:) DO appear with their titles;
//   * titles capped at 200 chars; TabInfo shape stable for the desktop side;
//   * activeTab = active tab of the last-focused normal window, null when it
//     is the link tab or a private tab with the option off;
//   * the toolbar kill switch still refuses both methods.
// Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));

const tab = (o) => ({
  active: false, pinned: false, audible: false, incognito: false,
  windowType: "normal", index: 0, windowId: 1,
  title: "untitled", url: "https://example.com/", lastAccessed: 1700000000000,
  ...o,
});

function fakeBackground({ stored = {}, tabs = [], focusedWindowId = 1, windowsDown = false } = {}) {
  const noop = () => {};
  const browser = {
    runtime: {
      onMessage: { addListener: noop }, onStartup: { addListener: noop },
      onInstalled: { addListener: noop }, getManifest: () => ({ version: "0" }),
    },
    storage: {
      local: { get: async (d) => ({ ...d, ...stored }), set: async () => {} },
      onChanged: { addListener: noop },
    },
    tabs: {
      query: async (opts = {}) => tabs.filter((t) => {
        if (opts.windowType && t.windowType !== opts.windowType) return false;
        if (opts.windowId != null && t.windowId !== opts.windowId) return false;
        if (opts.active && !t.active) return false;
        if (opts.currentWindow && t.windowId !== focusedWindowId) return false;
        return true;
      }),
      get: async (id) => tabs.find((t) => t.id === id) || null,
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
    },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    alarms: { create: noop, onAlarm: { addListener: noop } },
    scripting: { executeScript: async () => {} },
  };
  const context = vm.createContext({ browser, console, setTimeout, clearTimeout, Date });
  vm.runInContext(readFileSync(path.join(here, "..", "background.js"), "utf8"), context);
  return context;
}

// One realistic multi-window desktop: window 1 has normal tabs + internal
// pages + the link tab (+ its error-page twin); window 2 is a private window.
const PORT = 8766;
const LINK_URL = `http://127.0.0.1:${PORT}/link`;
const LINK_ERR_URL = `data:text/plain,127.0.0.1%3A${PORT}%2Flink`;
const LONG_TITLE = "x".repeat(250);

function surveyTabs() {
  return [
    tab({ id: 11, index: 0, windowId: 1, title: "Wiki", url: "https://wiki.example.org/", active: true, lastAccessed: 111 }),
    tab({ id: 12, index: 1, windowId: 1, title: LONG_TITLE, url: "https://example.com/long", pinned: true, lastAccessed: 112 }),
    tab({ id: 13, index: 2, windowId: 1, title: "New Tab", url: "about:blank", lastAccessed: 113 }),
    tab({ id: 14, index: 3, windowId: 1, title: "Riko Helper", url: "moz-extension://abc/options.html", audible: true, lastAccessed: 114 }),
    tab({ id: 15, index: 4, windowId: 1, title: "Riko link", url: LINK_URL, lastAccessed: 115 }),
    tab({ id: 16, index: 5, windowId: 1, title: "", url: LINK_ERR_URL, lastAccessed: 116 }),
    tab({ id: 21, index: 0, windowId: 2, incognito: true, title: "Private bank", url: "https://bank.example/" }),
    tab({ id: 22, index: 1, windowId: 2, incognito: true, active: true, title: "Private news", url: "https://news.example/", lastAccessed: 222 }),
  ];
}

const SHAPE = ["id", "windowId", "title", "url", "active", "pinned", "audible",
               "incognito", "lastAccessed"];

test("listTabs: drops incognito tabs by default, keeps internal pages, drops the link tab", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, tabs: surveyTabs() });
  const out = await ctx.handleCommand("listTabs", {});
  assert.equal(out.ok, true);
  const ids = out.result.map((t) => t.id);
  assert.deepEqual(ids, [11, 12, 13, 14]);               // window order, index order
  assert.ok(!ids.includes(15), "the link tab leaked into listTabs");
  assert.ok(!ids.includes(16), "the link tab's error page leaked into listTabs");
  assert.ok(!ids.includes(21) && !ids.includes(22), "incognito tabs leaked with the option off");
  const internal = out.result.filter((t) => /^(about|moz-extension):/.test(t.url));
  assert.equal(internal.length, 2, "about:/moz-extension: pages stay with their titles");
});

test("listTabs: includes incognito tabs when shareIncognito is on", async () => {
  const ctx = fakeBackground({ stored: { port: PORT, shareIncognito: true }, tabs: surveyTabs() });
  const out = await ctx.handleCommand("listTabs", {});
  const ids = out.result.map((t) => t.id);
  assert.deepEqual(ids, [11, 12, 13, 14, 21, 22]);
  assert.equal(out.result.find((t) => t.id === 21).incognito, true);
  assert.ok(!ids.includes(15) && !ids.includes(16), "link tab stays filtered even with sharing on");
});

test("listTabs: TabInfo shape, 200-char title cap, lastAccessed passthrough/null", async () => {
  const tabs = surveyTabs().filter((t) => !t.incognito && t.id !== 15 && t.id !== 16);
  tabs[2].lastAccessed = undefined;                      // tab Firefox never touched
  const ctx = fakeBackground({ stored: { port: PORT }, tabs });
  const out = await ctx.handleCommand("listTabs", {});
  for (const t of out.result) {
    assert.deepEqual(Object.keys(t).sort(), [...SHAPE].sort(), `bad TabInfo shape: ${JSON.stringify(t)}`);
    assert.ok(t.title.length <= 200, `title not capped: ${t.title.length}`);
  }
  assert.equal(out.result.find((t) => t.id === 12).title, "x".repeat(200));
  assert.equal(out.result.find((t) => t.id === 11).lastAccessed, 111);
  assert.equal(out.result.find((t) => t.id === 13).lastAccessed, null);
  const pinned = out.result.find((t) => t.id === 12);
  assert.equal(pinned.pinned, true);
  assert.equal(out.result.find((t) => t.id === 14).audible, true);
});

test("activeTab: active tab of the last-focused normal window", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, tabs: surveyTabs(), focusedWindowId: 1 });
  const out = await ctx.handleCommand("activeTab", {});
  assert.equal(out.ok, true);
  assert.equal(out.result.id, 11);
  assert.deepEqual(Object.keys(out.result).sort(), [...SHAPE].sort());
  assert.equal(out.result.title, "Wiki");
  assert.equal(out.result.active, true);
});

test("activeTab: private window active tab is null with sharing off, real with it on", async () => {
  const off = fakeBackground({ stored: { port: PORT }, tabs: surveyTabs(), focusedWindowId: 2 });
  const offOut = await off.handleCommand("activeTab", {});
  assert.equal(offOut.ok, true);
  assert.equal(offOut.result, null);
  const on = fakeBackground({ stored: { port: PORT, shareIncognito: true },
                              tabs: surveyTabs(), focusedWindowId: 2 });
  const out = await on.handleCommand("activeTab", {});
  assert.equal(out.result.id, 22);
  assert.equal(out.result.incognito, true);
});

test("activeTab: the link tab itself is never reported", async () => {
  const tabs = surveyTabs().map((t) => t.id === 11 ? { ...t, active: false } : t)
    .concat(tab({ id: 31, index: 6, windowId: 1, active: true, title: "Riko link", url: LINK_URL }));
  const ctx = fakeBackground({ stored: { port: PORT }, tabs, focusedWindowId: 1 });
  const out = await ctx.handleCommand("activeTab", {});
  assert.equal(out.ok, true);
  assert.equal(out.result, null);
});

test("activeTab: null when no normal window answers", async () => {
  const ctx = fakeBackground({ stored: { port: PORT }, tabs: surveyTabs(), windowsDown: true });
  const out = await ctx.handleCommand("activeTab", {});
  assert.equal(out.ok, true);
  assert.equal(out.result, null);
});

test("paused kill switch refuses both survey methods too", async () => {
  const ctx = fakeBackground({ stored: { port: PORT, paused: true }, tabs: surveyTabs() });
  for (const m of ["listTabs", "activeTab"]) {
    const out = await ctx.handleCommand(m, {});
    assert.equal(out.ok, false);
    assert.match(out.error, /paused/);
  }
});
