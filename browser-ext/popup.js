"use strict";

// The toolbar kill switch: paused = every command answers "paused". This is
// the one control that works with no app and no token, so it can always
// rescue you.
const $ = (id) => document.getElementById(id);

// Desktop line: probe /hello ourselves for a fresh answer (the background's
// 30 s 'riko-status' alarm keeps storage warm for the offline case).
async function probeDesktop() {
  const s = await browser.storage.local.get({ port: 8766 });
  let online = false;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 1500);
    const res = await fetch(`http://127.0.0.1:${s.port}/hello`,
                            { cache: "no-store", signal: ctl.signal });
    const j = res.ok ? await res.json() : null;
    clearTimeout(timer);
    online = !!(j && j.ok === true && j.name === "riko-desktop");
  } catch { /* offline */ }
  $("desktop").textContent = "Desktop: " + (online ? "online" : "offline");
  return online;
}

async function refresh() {
  const s = await browser.storage.local.get({ paused: false, link: "down" });
  $("state").textContent = s.paused
    ? "⏸ paused — she can't touch this Firefox"
    : (s.link === "up" ? "▶ live — linked to the Riko desktop"
                       : s.link === "reject" ? "🔑 wrong token (options page)"
                                             : "waiting for the desktop link");
  $("detail").textContent = s.link === "up" || s.paused
    ? "" : "start the Riko desktop app, then press Connect";
  $("toggle").textContent = s.paused ? "Resume" : "Pause her";
  browser.action.setBadgeText({ text: s.paused ? "⏸" : "" });
}

$("toggle").addEventListener("click", async () => {
  const s = await browser.storage.local.get({ paused: false });
  await browser.storage.local.set({ paused: !s.paused });
  refresh();
});

$("connect").addEventListener("click", async () => {
  $("connect").disabled = true;
  $("connect").textContent = "Connecting…";
  const answer = await browser.runtime.sendMessage({ cmd: "connect" })
    .catch(() => ({ online: false }));
  $("desktop").textContent = "Desktop: " + (answer.online ? "online" : "offline");
  $("connect").disabled = false;
  $("connect").textContent = "Connect";
  setTimeout(refresh, 400);   // give the link tab a moment to report in
});

browser.storage.onChanged.addListener((changes) => {
  if ("desktopOnline" in changes) {
    $("desktop").textContent = "Desktop: " + (changes.desktopOnline.newValue ? "online" : "offline");
  }
  if ("link" in changes) refresh();
});

refresh();
probeDesktop();
