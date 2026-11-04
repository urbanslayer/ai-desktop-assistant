/* Riko Helper — background page: command executor + link supervisor.
 *
 * The WebSocket to the Riko desktop (riko/client_tools.py BrowserBridge)
 * deliberately does NOT live in this page: Firefox 157 upgrades ws:// from
 * secure (moz-extension) contexts to wss:// with no fallback, and our bridge
 * is plaintext loopback on purpose. So the socket lives in a content script
 * (link_relay.js) inside a quiet tab the desktop serves itself at
 * http://127.0.0.1:<port>/link — an http origin, which is never upgraded.
 *
 * Protocol on this side:
 *   link tab -> us:  {riko: {id, method, params}}  -> {id, ok:true, result}
 *                    | {id, ok:false, error} | {id, ok:false,
 *                    needs_confirmation:true, reason}
 *   link tab -> us:  {rikoState: "up"|"down"|"reject"} (drives the badge)
 *
 * Safety rails that live HERE (not in the app, so a rogue app can't bypass):
 *   - toolbar kill switch (popup toggle): paused answers every command
 *     with "paused".
 *   - per-site allow list from the options page: empty = all sites, else only
 *     listed origins accept commands.
 *   - the risk / password rules live in content.js and lib/risk.js.
 */
"use strict";

let lastReadTabId = null;       // where the last browser_read happened
let linkState = "down";         // what the link tab's WebSocket says
const settingsCache = { token: "", port: 8766, paused: false, allowlist: [] };

const storageGet = async () => {
  const got = await browser.storage.local.get({
    token: "", port: 8766, paused: false, allowlist: [],
  });
  Object.assign(settingsCache, got);
  return got;
};

const setBadge = (text, color = "#ffb020") => {
  browser.action.setBadgeBackgroundColor({ color }).catch(() => {});
  browser.action.setBadgeText({ text }).catch(() => {});
};

// ── Messages from the link tab ───────────────────────────────
browser.runtime.onMessage.addListener((msg) => {
  if (msg && msg.rikoState) {
    linkState = String(msg.rikoState);
    if (linkState === "up") setBadge("✓", "#2e7d32");
    else if (linkState === "reject") setBadge("🔑");   // wrong token: owner fix
    else setBadge("");
    browser.storage.local.set({ link: linkState }).catch(() => {});
    return;
  }
  if (msg && msg.riko && msg.riko.id != null) {
    const { id, method, params } = msg.riko;
    return handleCommand(method, params || {}).then((answer) => ({ id, ...answer }));
  }
});

// ── The link tab ─────────────────────────────────────────────
const LINK_PREFIX = () => `http://127.0.0.1:${settingsCache.port}/link`;
const isLinkTab = (t) =>
  (t.url || "").startsWith(LINK_PREFIX()) ||
  (t.url || "").includes(`127.0.0.1%3A${settingsCache.port}%2Flink`); // error page

async function ensureLinkTab() {
  await storageGet();
  const tabs = await browser.tabs.query({});
  const hit = tabs.find(isLinkTab);
  if (hit) {
    if (linkState !== "up" && !(hit.url || "").startsWith("about:")) {
      await browser.tabs.reload(hit.id).catch(() => {});  // stalled relay
    }
    return;
  }
  const tab = await browser.tabs.create({ url: LINK_PREFIX(), active: false });
  try { await browser.tabs.hide(tab.id); } catch { /* headless or last window */ }
}

// ── Tabs (list, resolve, banners) ────────────────────────────
async function windowTabs() {
  const tabs = await browser.tabs.query({ currentWindow: true });
  return tabs.filter((t) => !isLinkTab(t)).sort((a, b) => a.index - b.index);
}

async function resolveTab(number) {
  const tabs = await windowTabs();
  if (number == null) {
    if (lastReadTabId != null) {
      const t = await browser.tabs.get(lastReadTabId).catch(() => null);
      if (t) return t;
    }
    return tabs.find((t) => t.active) || tabs[0];
  }
  const n = Math.trunc(number);
  const hit = n >= 1 ? tabs[n - 1] : tabs[n];  // browser_tabs shows 1-based numbers
  if (!hit) throw new Error(`there is no tab ${number} in this window`);
  return hit;
}

function allowed(url) {
  const list = settingsCache.allowlist || [];
  if (!list.length) return true;
  let origin = "";
  try { origin = new URL(url).origin; } catch { return false; }
  return list.some((o) => o.trim() && origin === o.trim().replace(/\/$/, ""));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Firefox rejects tabs.sendMessage with "Could not establish connection.
// Receiving end does not exist." when no content-script listener is registered
// in the tab yet. That error class PROVES the command never ran in the page,
// so re-sending it is safe (no double clicks). "Message manager closed" is the
// frame-is-not-ready sibling (fresh tab; Firefox 157 sometimes answers this
// before the content script even exists) — also pre-dispatch, also safe.
// Anything else (restricted page, missing site permission) will not be fixed
// by injecting, so it must not be retried or injected on.
const NO_RECEIVING_END = /receiving end does not exist|could not establish connection/i;
const PRE_DISPATCH_RETRYABLE = /message manager closed/i;

async function toTab(tabId, msg) {
  let lastError = null;
  const errText = () => String((lastError && lastError.message) || lastError);
  const retryable = () =>
    NO_RECEIVING_END.test(errText()) || PRE_DISPATCH_RETRYABLE.test(errText());
  const fail = () => new Error(`no access to that tab (site permission?): ${errText()}`);
  const attempt = async () => {
    try {
      return { res: await browser.tabs.sendMessage(tabId, msg) };
    } catch (e) {
      lastError = e;
      return null;
    }
  };
  // poll for the content script instead of sleeping blind: on Firefox 157 a
  // fresh tab's declared content script sometimes registers late
  const poll = async (budgetMs) => {
    const deadline = Date.now() + budgetMs;
    for (;;) {
      const r = await attempt();
      if (r) return { done: true, res: r.res };
      if (!retryable()) return { done: false, err: fail() };
      if (Date.now() >= deadline) return { done: false };
      await sleep(250);
    }
  };

  let out = await poll(2500);
  if (out.done) return out.res;
  if (out.err) throw out.err;

  if (!NO_RECEIVING_END.test(errText())) throw fail();  // not an injectable state
  // page loaded before the extension existed: inject ONCE per call. risk.js,
  // snapshot.js and content.js are idempotent (window-scoped guards), so
  // racing a slow-but-present declared content script still leaves exactly
  // one onMessage listener and stable data-riko-id numbering.
  try {
    await browser.scripting.executeScript({ target: { tabId },
      files: ["lib/risk.js", "lib/snapshot.js", "content.js"] });
  } catch (e) {
    lastError = e;
    throw fail();
  }
  out = await poll(2000);
  if (out.done) return out.res;
  throw out.err || fail();
}

const banner = (tabId, on) => browser.tabs.sendMessage(tabId, { cmd: "banner", on }).catch(() => {});

// ── The tools ────────────────────────────────────────────────
async function handleCommand(method, params) {
  await storageGet();
  if (settingsCache.paused) return { ok: false, error: "Riko Helper is paused — click its toolbar icon to resume" };
  try {
    return await ({
      tabs: doTabs, open: doOpen, read: doRead, click: doClick, type: doType,
      select: doSelect, scroll: doScroll, back: doBack, screenshot: doScreenshot,
      close: doClose,
    })[method](params);
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}

async function doTabs() {
  const tabs = await windowTabs();
  return { ok: true, result: tabs.map((t, i) => ({
    n: i + 1, title: t.title || "", url: t.url || "", active: !!t.active,
  })) };
}

async function doOpen(params) {
  const url = String(params.url || "");
  if (!/^https?:\/\//i.test(url)) return { ok: false, error: "only http(s) URLs" };
  if (params.new_tab === false) {
    const tab = await resolveTab(null);
    await browser.tabs.update(tab.id, { url, active: true });
    lastReadTabId = tab.id;
    return { ok: true, result: `replaced the active tab with ${url}` };
  }
  const tab = await browser.tabs.create({ url, active: true });
  lastReadTabId = tab.id;
  return { ok: true, result: `opened ${url} in a new tab` };
}

async function doRead(params) {
  const tab = await resolveTab(params.tab);
  if (!allowed(tab.url)) return { ok: false, error: `${tab.url} is not on the allow list` };
  lastReadTabId = tab.id;
  const snap = await toTab(tab.id, { cmd: "snapshot" });
  return { ok: true, result: snap };
}

async function doAct(params, kind) {
  const tab = await resolveTab(null);
  if (!allowed(tab.url)) return { ok: false, error: `${tab.url} is not on the allow list` };
  lastReadTabId = tab.id;
  await banner(tab.id, true);
  try {
    const out = await toTab(tab.id, { cmd: "act", kind, ...params, n: params.n, id: params.n });
    return out;
  } finally {
    banner(tab.id, false);
  }
}

const doClick = (p) => doAct({ n: p.n, confirmed: p.confirmed }, "click");
const doType = (p) => doAct({ n: p.n, text: p.text, submit: p.submit, confirmed: p.confirmed }, "type");
const doSelect = (p) => doAct({ n: p.n, value: p.value, confirmed: p.confirmed }, "select");

async function doScroll(params) {
  const tab = await resolveTab(null);
  if (!allowed(tab.url)) return { ok: false, error: `${tab.url} is not on the allow list` };
  await banner(tab.id, true);
  try {
    return await toTab(tab.id, { cmd: "scroll", direction: params.direction || "down",
                                 amount: params.amount });
  } finally {
    banner(tab.id, false);
  }
}

async function doBack(params) {
  const tab = await resolveTab(params.tab);
  await browser.tabs.goBack(tab.id);
  return { ok: true, result: `went back on ${tab.title || tab.url}` };
}

async function doScreenshot(params) {
  const tab = await resolveTab(params.tab);
  if (!tab.active) await browser.tabs.update(tab.id, { active: true });
  const data = await browser.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  return { ok: true, result: data };  // data:image/png;base64,...
}

async function doClose(params) {
  const tab = await resolveTab(params.tab);
  await browser.tabs.remove(tab.id);
  if (lastReadTabId === tab.id) lastReadTabId = null;
  return { ok: true, result: `closed ${tab.title || tab.url}` };
}

// ── Startup & link supervision ───────────────────────────────
browser.runtime.onStartup.addListener(() => { storageGet().then(ensureLinkTab); });
browser.runtime.onInstalled.addListener(() => { storageGet().then(ensureLinkTab); });
browser.tabs.onRemoved.addListener(() => {
  // cheap no-op unless the link tab was the one that vanished
  setTimeout(ensureLinkTab, 1200);
});
browser.storage.onChanged.addListener((changes) => {
  if ("port" in changes) { linkState = "down"; storageGet().then(ensureLinkTab); }
});
browser.alarms.create("riko-link", { periodInMinutes: 1 });
browser.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== "riko-link") return;
  await storageGet();
  if (linkState === "up") return;
  await ensureLinkTab();
});
storageGet().then(ensureLinkTab);
