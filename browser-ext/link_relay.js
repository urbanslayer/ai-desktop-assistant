/* Riko Helper — link relay.
 *
 * Runs inside the quiet tab the Riko desktop serves itself
 * (http://127.0.0.1:<port>/link). That page's origin is http, so Firefox
 * leaves our ws:// alone (from the secure extension context it would be
 * force-upgraded to wss:// with no fallback). Everything else — commands,
 * the kill switch, the allow list — lives in the background page; we are
 * just the WebSocket.
 */
"use strict";
(() => {
  if (window.__rikoLink) return;   // survive double-injection
  window.__rikoLink = true;

  const port = location.port || "8766";
  let ws = null, retry = 0;

  const state = (s) => {
    browser.runtime.sendMessage({ rikoState: s }).catch(() => {});
    window.postMessage({ source: "riko-link", state: s }, "*");
  };

  async function getToken() {
    // same-origin endpoint the desktop serves; localhost-only by construction
    try {
      const res = await fetch("/riko-pair", { cache: "no-store" });
      if (res.ok) {
        const t = (await res.text()).trim();
        if (t) return t;
      }
    } catch { /* desktop restarting; fall through to stored token */ }
    const s = await browser.storage.local.get({ token: "" });
    return s.token || "";
  }

  function dial() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
    ws = new WebSocket(`ws://127.0.0.1:${port}/`);
    ws.onopen = async () => {
      retry = 0;
      ws.send(JSON.stringify({
        type: "hello", token: await getToken(),
        name: "Riko Helper", version: browser.runtime.getManifest().version,
      }));
    };
    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "hello_ok") { state("up"); return; }
      if (msg.type === "hello_reject") {
        state("reject");
        setTimeout(() => location.reload(), 15000);   // token may have changed
        return;
      }
      if (msg.id != null) {
        const answer = await browser.runtime.sendMessage({ riko: msg })
          .catch((e) => ({ id: msg.id, ok: false, error: String(e && e.message || e) }));
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(answer || { id: msg.id, ok: false, error: "no answer" }));
        }
      }
    };
    ws.onclose = () => {
      state("down");
      ws = null;
      retry = Math.min(Math.max(retry * 2, 2000), 30000);
      setTimeout(dial, retry);
    };
    ws.onerror = () => { try { ws.close(); } catch { /* already gone */ } };
  }

  dial();
})();
