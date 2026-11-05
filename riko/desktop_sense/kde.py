"""KDE Plasma backend: a tiny KWin script loaded at runtime over D-Bus
(org.kde.KWin /Scripting), reporting windows/monitors through a temp JSON
file, then unloaded (contract §2). Focus rides the same mechanism.

KWin 5 and Plasma 6 scripting differ; the script probes both shapes and
fails loudly otherwise. Launching uses the generic gtk-launch/gio path —
moving a window to a specific screen after it maps is not reliably
scriptable, so placement says so (best-effort per the contract).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .api_errors import ToolError
from .util import clean_title

_SCRIPT_SRC = r"""
function rikoWins() {
    var wins = [];
    try { wins = workspace.windowList(); } catch (e) {
        try { wins = workspace.windows; } catch (e2) { wins = []; }
    }
    return wins || [];
}
function rikoWin(w) {
    var out = "";
    try { out = (w.output !== undefined && w.output !== null) ? String(w.output) : ""; } catch (e) {}
    if (!out) { try { out = String(w.screen); } catch (e) {} }
    var ws = "";
    try { ws = String(w.desktop !== undefined ? w.desktop : ""); } catch (e) {}
    return {
        id: String(w.internalId !== undefined ? w.internalId : (w.windowId || "")),
        app: String(w.resourceClass || ""),
        title: String(w.caption || ""),
        workspace: ws,
        monitor: out,
        focused: !!w.active,
        fullscreen: !!(w.fullScreen || w.fullscreen),
        floating: false,
        pid: (w.pid !== undefined ? w.pid : null)
    };
}
function rikoCurDesktop() {
    // Plasma 6: currentDesktop is a Workspace/Desk object (id/name);
    // KWin 5: a plain number
    try {
        var cd = workspace.currentDesktop;
        if (cd === undefined || cd === null) return "";
        if (cd.desktopId !== undefined) return String(cd.desktopId);
        if (cd.name !== undefined && String(cd.name)) return String(cd.name);
        return String(cd);
    } catch (e) { return ""; }
}
function rikoList() {
    var list = [];
    var wins = rikoWins();
    for (var i = 0; i < wins.length; i++) {
        try { list.push(rikoWin(wins[i])); } catch (e) {}
    }
    var screens = [];
    var cur = rikoCurDesktop();
    try {
        var ss = workspace.screens || [];
        for (var s = 0; s < ss.length; s++) {
            var g = ss[s].geometry || {};
            screens.push({name: String(ss[s].name || ("screen-" + s)),
                          description: (String(ss[s].manufacturer || "") + " " +
                                        String(ss[s].model || "")).trim(),
                          size: (g.width || 0) + "x" + (g.height || 0),
                          position: (g.x || 0) + "x" + (g.y || 0),
                          scale: Number(ss[s].scale || 1),
                          focused: false, active_workspace: cur});
        }
    } catch (e) {}
    var focus = null;
    try { if (workspace.activeWindow) focus = rikoWin(workspace.activeWindow); } catch (e) {}
    return JSON.stringify({ok: true, windows: list, monitors: screens, focused: focus});
}
function rikoSwitchWorkspace(ref) {
    // '2' / 'desktop 2' -> the 2nd virtual desktop; names match Plasma 6
    // desktop names. KWin 5 uses numeric currentDesktop, Plasma 6 a Desk.
    ref = String(ref || "");
    var m = ref.match(/(\d+)/);
    var ds = null;
    try { ds = workspace.desktops ? workspace.desktops() : null; } catch (e) {}
    if (ds && ds.length) {
        var i = -1;
        for (var k = 0; k < ds.length; k++) {
            if (String(ds[k].name || "") === ref) { i = k; break; }
        }
        if (i < 0 && m) i = parseInt(m[1], 10) - 1;
        if (i < 0 || i >= ds.length)
            return JSON.stringify({ok: false, error: "no desktop " + ref});
        try { workspace.currentDesktop = ds[i];
              return JSON.stringify({ok: true, workspace: ref}); }
        catch (e2) { return JSON.stringify({ok: false, error: String(e2)}); }
    }
    if (!m) return JSON.stringify({ok: false, error: "no desktop number in " + ref});
    var n = parseInt(m[1], 10) - 1;
    try { workspace.currentDesktop = n;
          return JSON.stringify({ok: true, workspace: ref}); }
    catch (e3) { return JSON.stringify({ok: false, error: String(e3)}); }
}
function rikoFocus(ref) {
    var wins = rikoWins();
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var id = String(w.internalId !== undefined ? w.internalId : (w.windowId || ""));
        if (id === ref || String(w.caption) === ref || String(w.resourceClass) === ref) {
            try { workspace.activeWindow = w; }
            catch (e) { return JSON.stringify({ok: false, error: String(e)}); }
            return JSON.stringify({ok: true});
        }
    }
    return JSON.stringify({ok: false, error: "no window matches " + ref});
}
"""


def _env_ready(env=None):
    e = env if env is not None else os.environ
    desk = (e.get("XDG_CURRENT_DESKTOP") or "").lower()
    return ("kde" in desk or bool(e.get("KDE_SESSION_VERSION"))) and \
        bool(shutil.which("qdbus") or shutil.which("dbus-send"))


class KWinBackend:
    name = "kde"
    supports_launch = False   # launch falls back to the generic launcher

    def __init__(self, runner=None, env=None):
        self.runner = runner
        self.env = env if env is not None else os.environ

    # ── D-Bus plumbing ─────────────────────────────────────────
    def _dbus(self, member, args=(), runner=None, timeout=10):
        from .util import run
        q = shutil.which("qdbus")
        if q:
            argv = [q, "--session", "org.kde.KWin", "/Scripting",
                    f"org.kde.kwin.Scripting.{member}", *[str(a) for a in args]]
        else:
            sig = "s" * len(args)
            argv = ["dbus-send", "--session", "--print-reply",
                    "--dest=org.kde.KWin", "/Scripting",
                    f"org.kde.kwin.Scripting.{member}" + (f":{sig}" if sig else ""),
                    *[str(a) for a in args]]
        try:
            done = run(argv, timeout=timeout,
                       runner=runner if runner is not None else self.runner)
        except FileNotFoundError:
            raise ToolError("no D-Bus tool (qdbus/dbus-send) to reach KWin") from None
        except subprocess.TimeoutExpired:
            raise ToolError("KWin did not answer over D-Bus") from None
        if done.returncode != 0:
            raise ToolError(f"KWin D-Bus {member} failed: "
                            f"{(done.stderr or done.stdout or '').strip()[:200]}")
        return done.stdout or ""

    def _run_script(self, expr, runner=None, wait=4.0):
        """Load + run a KWin script that writes `expr`'s JSON to a temp file.
        `expr` is JS source evaluated in _SCRIPT_SRC's scope (rikoList()…)."""
        out = Path(f"/tmp/riko-kwin-{os.getpid()}-{secrets.token_hex(3)}.json")
        sid = f"riko_{os.getpid()}_{secrets.token_hex(3)}"
        script = Path(tempfile.mkstemp(suffix=".js")[1])
        script.write_text(_SCRIPT_SRC +
                          f"\ntry {{ writeFile({json.dumps(str(out))}, {expr}); }}"
                          " catch (e) {}", encoding="utf-8")
        try:
            self._dbus("loadScriptForIdentifier", [str(script), sid], runner=runner)
            self._dbus("run", [sid], runner=runner)
            deadline = time.monotonic() + wait
            data = None
            while time.monotonic() < deadline:
                try:
                    data = json.loads(out.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    time.sleep(0.15)
            if data is None:
                raise ToolError("the KWin script produced no result — this KWin's "
                                "scripting API may differ (KDE unverified)")
            return data
        finally:
            try:
                self._dbus("unloadScript", [sid], runner=runner)
            except ToolError:
                pass
            for p in (script, out):
                try:
                    p.unlink()
                except OSError:
                    pass

    # ── state ──────────────────────────────────────────────────
    def _normalize(self, w):
        return {"id": w.get("id", ""), "app": w.get("app", ""),
                "title": clean_title(w.get("title", "")),
                "workspace": str(w.get("workspace", "")),
                "monitor": str(w.get("monitor", "")),
                "focused": bool(w.get("focused")),
                # KWin scripting has no focus history: only the active
                # window has a known rank
                "focus_recency": 0 if w.get("focused") else None,
                "fullscreen": bool(w.get("fullscreen")),
                "floating": bool(w.get("floating")), "pid": w.get("pid")}

    def windows(self) -> list[dict]:
        data = self._run_script("rikoList()")
        return [self._normalize(w) for w in data.get("windows") or []]

    def focused(self) -> dict | None:
        w = self._run_script("rikoList()").get("focused")
        return self._normalize(w) if w else None

    def monitors(self) -> list[dict]:
        return self._run_script("rikoList()").get("monitors") or []

    # ── actions ────────────────────────────────────────────────
    def switch_workspace(self, workspace, monitor=None) -> dict:
        """Virtual desktops via the same KWin script:
        `workspace.currentDesktop = desktops[n-1]` (Plasma 6) or the
        desktop number (KWin 5). KWin cannot move one desktop onto a
        specific screen — screens show all desktops — so a monitor is
        answered with a note, not an error."""
        ref = str(workspace or "").strip()
        if not ref:
            raise ToolError("switch_workspace: give a desktop number or name")
        data = self._run_script(f"rikoSwitchWorkspace({json.dumps(ref)})")
        if not data.get("ok"):
            raise ToolError(data.get("error") or
                            "KWin could not switch to that desktop")
        out = {"workspace": ref, "monitor": monitor or ""}
        if monitor:
            out["note"] = ("KDE shows every desktop on every screen; switched "
                           "on the whole desktop (no per-screen placement)")
        return out

    def focus_window(self, ref: str) -> dict:
        data = self._run_script(f"rikoFocus({json.dumps(str(ref))})")
        if not data.get("ok"):
            raise ToolError(data.get("error") or "KWin could not focus that window")
        return {"focused": str(ref)}
