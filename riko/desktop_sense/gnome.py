"""GNOME (Mutter) backend (contract §3).

Wayland GNOME exposes no window list to normal apps, so we ship a tiny
Shell extension (desktop/gnome-extension/riko-windows@riko) that exports a
session-D-Bus service `im.riko.Windows` with List/Activate/Focused/Monitors
reading global.get_window_actors(). This module is its D-Bus client
(qdbus/dbus-send).

Without the extension: only desktop_apps/open_app (generic launcher) and
the browser tools work — the state tools say exactly why.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from .api_errors import ToolError
from .util import clean_title

BUS = "im.riko.Windows"
PATH = "/im/riko/Windows"
INSTALL_HINT = ("install Riko's GNOME Shell extension to let her see "
                "windows: copy desktop/gnome-extension/riko-windows@riko to "
                "~/.local/share/gnome-shell/extensions/ and restart GNOME "
                "(see README 'She can see your desktop')")


def _env_ready(env=None):
    e = env if env is not None else os.environ
    return "gnome" in (e.get("XDG_CURRENT_DESKTOP") or "").lower() and \
        bool(shutil.which("qdbus") or shutil.which("dbus-send"))


class GnomeBackend:
    name = "gnome"
    supports_launch = False   # generic gtk-launch/gio path

    def __init__(self, runner=None, env=None):
        self.runner = runner
        self.env = env if env is not None else os.environ

    def _call(self, method, sig="", args=(), runner=None):
        from .util import run
        q = shutil.which("qdbus")
        if q:
            argv = [q, "--session", BUS, PATH, f"{BUS}.{method}",
                    *[str(a) for a in args]]
        else:
            argv = ["dbus-send", "--session", "--print-reply", f"--dest={BUS}",
                    PATH, f"{BUS}.{method}" + (f":{sig}" if sig else ""),
                    *[str(a) for a in args]]
        try:
            done = run(argv, timeout=10,
                       runner=runner if runner is not None else self.runner)
        except FileNotFoundError:
            raise ToolError("no D-Bus tool (qdbus/dbus-send) to reach GNOME") from None
        except subprocess.TimeoutExpired:
            raise ToolError("the GNOME Shell extension did not answer") from None
        if done.returncode != 0:
            raise ToolError(INSTALL_HINT)
        return done.stdout or ""

    @staticmethod
    def _json_from_reply(reply: str):
        # qdbus prints the plain string; dbus-send prints `string "..."`
        m = re.search(r'string\s+"(.*)"\s*$', reply, re.S)
        raw = m.group(1) if m else reply.strip()
        try:
            return json.loads(raw)
        except ValueError:
            raise ToolError("the GNOME Shell extension answered something odd") from None

    def _list(self, runner=None):
        return self._json_from_reply(self._call("List", runner=runner))

    def windows(self) -> list[dict]:
        out = []
        for w in self._list().get("windows") or []:
            out.append({"id": str(w.get("id", "")), "app": w.get("app", ""),
                        "title": clean_title(w.get("title", "")),
                        "workspace": str(w.get("workspace", "")),
                        "monitor": str(w.get("monitor", "")),
                        "focused": bool(w.get("focused")),
                        "fullscreen": bool(w.get("fullscreen")),
                        "floating": bool(w.get("floating")),
                        "pid": w.get("pid")})
        return out

    def focused(self) -> dict | None:
        w = self._list().get("focused")
        if not w:
            return None
        return {"id": str(w.get("id", "")), "app": w.get("app", ""),
                "title": clean_title(w.get("title", "")),
                "workspace": str(w.get("workspace", "")),
                "monitor": str(w.get("monitor", "")), "focused": True,
                "fullscreen": bool(w.get("fullscreen")),
                "floating": bool(w.get("floating")), "pid": w.get("pid")}

    def monitors(self) -> list[dict]:
        data = self._json_from_reply(self._call("Monitors"))
        return data.get("monitors") or []

    def focus_window(self, ref: str) -> dict:
        wins = self.windows()
        ref_l = str(ref or "").strip().lower()
        win = next((w for w in wins if str(w["id"]).lower() == ref_l), None)
        if win is None:
            matches = [w for w in wins
                       if ref_l in (w.get("title") or "").lower()
                       or ref_l == (w.get("app") or "").lower()]
            win = matches[0] if len(matches) == 1 else (matches or [None])[0]
        if win is None:
            raise ToolError(f"no open window matches {ref!r}")
        data = self._json_from_reply(self._call("Activate", "s", [str(win["id"])]))
        if not data.get("ok"):
            raise ToolError(data.get("error") or "GNOME could not activate that window")
        return {"focused": win.get("app") or ref, "title": win.get("title", "")}
