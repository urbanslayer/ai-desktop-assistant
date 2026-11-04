"use strict";

// The toolbar kill switch: paused = every command answers "paused". This is
// the one control that works with no app and no token, so it can always
// rescue you.
async function refresh() {
  const s = await browser.storage.local.get({ paused: false, link: "down" });
  document.getElementById("state").textContent = s.paused
    ? "⏸ paused — she can't touch this Firefox"
    : (s.link === "up" ? "▶ live — linked to the Riko desktop"
                       : s.link === "reject" ? "🔑 wrong token (options page)"
                                             : "waiting for the desktop link tab");
  document.getElementById("detail").textContent = s.link === "up" || s.paused
    ? "" : "start the Riko desktop app; the link tab opens by itself";
  document.getElementById("toggle").textContent = s.paused ? "Resume" : "Pause her";
  browser.action.setBadgeText({ text: s.paused ? "⏸" : "" });
}

document.getElementById("toggle").addEventListener("click", async () => {
  const s = await browser.storage.local.get({ paused: false });
  await browser.storage.local.set({ paused: !s.paused });
  refresh();
});

refresh();
