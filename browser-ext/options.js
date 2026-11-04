"use strict";

// Pairing via URL: options.html?token=...&port=... saves instantly. The
// headless check uses this (`web-ext run --start-url options.html?token=...`),
// and it doubles as a one-click pairing link later.
const params = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await browser.storage.local.get({ token: "", port: 8766, allowlist: [],
                                              autoReopen: true });
  console.log(`[riko-helper] options loaded, query token=${params.get("token") ? "present" : "none"} stored=${s.token ? "set" : "empty"}`);
  $("token").value = params.get("token") || s.token;
  $("port").value = params.get("port") || s.port;
  $("allowlist").value = (s.allowlist || []).join("\n");
  $("auto_reopen").checked = s.autoReopen !== false;
  const granted = await browser.permissions.contains({ origins: ["<all_urls>"] });
  $("perm").textContent = granted ? "All sites allowed ✓" : "Allow reading all sites";
  if (params.get("token") && params.get("token") !== s.token) {
    await save();
    $("status").textContent = "Paired from the link — you can close this tab.";
  }
}

async function save() {
  const token = $("token").value.trim();
  const port = Math.trunc(Number($("port").value) || 8766);
  const allowlist = $("allowlist").value.split("\n").map((s) => s.trim()).filter(Boolean);
  for (const o of allowlist) {
    try { new URL(o); } catch { $("status").textContent = `not a valid origin: ${o}`; return; }
  }
  await browser.storage.local.set({ token, port, allowlist,
                                    autoReopen: $("auto_reopen").checked });
  $("status").textContent = token
    ? `Saved. Reconnecting to ws://127.0.0.1:${port} …`
    : "Saved (no token yet — paste one to pair).";
}

$("save").addEventListener("click", save);
$("pair").addEventListener("click", async () => {
  const port = Math.trunc(Number($("port").value) || 8766);
  try {
    const res = await fetch(`http://127.0.0.1:${port}/riko-pair`, { cache: "no-store" });
    const token = (await res.text()).trim();
    if (!res.ok || !token) throw new Error(String(res.status));
    $("token").value = token;
    await save();
    $("status").textContent = "Paired with the Riko desktop ✓";
  } catch {
    $("status").textContent = `No Riko desktop on port ${port} — start it, or paste the token.`;
  }
});
$("perm").addEventListener("click", async () => {
  const ok = await browser.permissions.request({ origins: ["<all_urls>"] });
  $("perm").textContent = ok ? "All sites allowed ✓" : "You said no (Riko will see less)";
});

load();
