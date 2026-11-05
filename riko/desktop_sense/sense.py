"""DesktopSense: backend detection + the tool surface for docs/DESKTOP_SENSE.md.

The tool specs and executors produced here are handed to
`riko.client_tools`, which registers them on the hub as `desktop__*` while
the awareness toggle is on. `browser_tabs` also lives here now: it calls
the extension's new `listTabs` method and falls back to the old `tabs`
answer, telling the owner to update the Riko Helper extension when an old
one knows neither.

Everything is lazy: detection happens on first use, and every subprocess
goes through a runner the tests can replace — the unit suite never touches
the owner's compositor.
"""
from __future__ import annotations

import os
import re

from .api_errors import ToolError
from .apps import AppIndex, open_app as apps_open_app
from .awareness import Awareness
from .monitors import resolve_monitor_phrase
from .util import clean_title

FIREFOX_CLASSES = re.compile(r"firefox|thunderbird", re.I)

# tools the owner approved without confirmation (docs/DESKTOP_SENSE.md,
# ff78db1): risk stays `act`, but the hub default permission is `always`.
ALWAYS_PERMISSION_TOOLS = ("open_app", "focus_window")


def detect_backend(env=None, runner=None):
    """First backend that is present (contract order: Hyprland, KDE, GNOME,
    Sway — Sway before the generic layer since it's cheap and exact)."""
    e = env if env is not None else os.environ
    forced = str(e.get("RIKO_DESKTOP_BACKEND") or "").strip().lower()
    from . import gnome, hyprland, kde, sway
    makers = {"hyprland": lambda: hyprland.HyprlandBackend(runner=runner, env=e),
              "sway": lambda: sway.SwayBackend(runner=runner, env=e),
              "kde": lambda: kde.KWinBackend(runner=runner, env=e),
              "gnome": lambda: gnome.GnomeBackend(runner=runner, env=e)}
    if forced in makers:
        return makers[forced]()
    if hyprland._env_ready(e):
        return makers["hyprland"]()
    if sway._env_ready(e):
        return makers["sway"]()
    if kde._env_ready(e):
        return makers["kde"]()
    if gnome._env_ready(e):
        return makers["gnome"]()
    return None


def _obj(props, required=None):
    spec = {"type": "object", "additionalProperties": False,
            "properties": props}
    if required:
        spec["required"] = list(required)
    return spec


def sense_tools():
    """The desktop_* catalogue (browser_tabs is registered by
    client_tools.browser_tools(); its executor is replaced here)."""
    return [
        {"name": "desktop_windows", "risk": "read",
         "description": "The owner's open windows: app id/class, title, "
                        "workspace, monitor, focused, fullscreen/floating, pid. "
                        "Denylisted apps show as '<hidden>'.",
         "parameters": _obj({})},
        {"name": "desktop_focused", "risk": "read",
         "description": "The focused window (same shape as desktop_windows rows) "
                        "plus the focused browser tab (title+URL) when Firefox "
                        "is focused and the Riko Helper is linked.",
         "parameters": _obj({})},
        {"name": "desktop_monitors", "risk": "read",
         "description": "The screens: name, description/make/model, size, "
                        "position, scale, focused, active workspace. On "
                        "Hyprland also the configured layout from the owner's "
                        "Hyprland config.",
         "parameters": _obj({})},
        {"name": "desktop_apps", "risk": "read",
         "description": "Installed launchable apps (XDG .desktop files), "
                        "fuzzy-searched by name/keyword.",
         "parameters": _obj({"query": {"type": "string",
                                       "description": "what to look for; "
                                                      "empty = list many"}})},
        {"name": "open_app", "risk": "act", "permission_default": "always",
         "description": "Launch an installed app (desktop id or fuzzy name; "
                        "'terminal'/'browser'/'files' mean the owner's own "
                        "configured ones). monitor: name, description words or "
                        "left/middle/right; workspace where allowed.",
         "parameters": _obj({"app": {"type": "string"},
                             "monitor": {"type": "string",
                                         "description": "'left', 'right', "
                                                        "'the Dell on the right', "
                                                        "'the portable screen', or a name"},
                             "workspace": {"type": "string"}}),
         "required": ["app"]},
        {"name": "focus_window", "risk": "act", "permission_default": "always",
         "description": "Bring an open window to the front: window id, app id "
                        "or title substring from desktop_windows.",
         "parameters": _obj({"window": {"type": "string"}}),
         "required": ["window"]},
    ]


class DesktopSense:
    """The live tool surface. `bridge` (BrowserBridge or None) powers the
    focused-tab part and browser_tabs; `cfg` seeds awareness + app dirs."""

    def __init__(self, cfg=None, bridge=None, backend=None, awareness=None,
                 runner=None, env=None, app_dirs=None):
        self.cfg = cfg or {}
        self.bridge = bridge
        self._backend = backend
        self._backend_tried = backend is not None
        self.awareness = awareness or Awareness(cfg)
        self.runner = runner
        self.env = env if env is not None else os.environ
        self.index = AppIndex(app_dirs)

    # ── backend ────────────────────────────────────────────────
    @property
    def backend(self):
        if not self._backend_tried:
            self._backend_tried = True
            self._backend = detect_backend(self.env, runner=self.runner)
        return self._backend

    def _need_backend(self):
        b = self.backend
        if b is None:
            raise ToolError("no supported window manager found (Hyprland, "
                            "Sway, KDE or GNOME Shell expected) — only "
                            "desktop_apps and open_app work here")
        return b

    def preferences(self) -> dict:
        b = self._backend if self._backend_tried else None
        if b is None and not self._backend_tried:
            b = self.backend
        return dict(getattr(b, "preferences", lambda: {})()) if b else {}

    # ── tabs via the Firefox helper ────────────────────────────
    def _bridge_call(self, method, params=None):
        if self.bridge is None:
            raise ToolError("the Firefox helper link is not running on this PC")
        from riko.client_tools import BrowserNotConnected
        try:
            answer = self.bridge.call(method, params or {})
        except BrowserNotConnected as e:
            raise ToolError(str(e)) from None
        return answer if isinstance(answer, dict) else {}

    @staticmethod
    def _tab_row(t: dict) -> dict:
        return {"id": t.get("id"), "window": t.get("windowId"),
                "title": clean_title(t.get("title", "")),
                "url": str(t.get("url", ""))[:300],
                "active": bool(t.get("active")),
                "pinned": bool(t.get("pinned")),
                "audible": bool(t.get("audible")),
                "private": bool(t.get("incognito"))}

    def browser_tabs(self) -> list[dict]:
        answer = self._bridge_call("listTabs")
        if not answer.get("ok") and _unknown_method(answer):
            answer = self._bridge_call("tabs")   # old extensions
            if answer.get("ok"):
                rows = [self._tab_row({"id": i + 1, "title": r.get("title"),
                                       "url": r.get("url"),
                                       "active": r.get("active")})
                        for i, r in enumerate(answer.get("result") or [])]
                return [r for r in rows if not (r["private"] and
                                                not self._show_private())] or rows
            raise ToolError("the Riko Helper extension is too old for tab "
                            "lists — update the Riko Helper extension")
        if not answer.get("ok"):
            raise ToolError(str(answer.get("error") or "the browser tool failed"))
        rows = [self._tab_row(t) for t in answer.get("result") or []]
        return [r for r in rows if not (r["private"] and not self._show_private())]

    def active_tab(self) -> dict | None:
        answer = self._bridge_call("activeTab")
        if not answer.get("ok"):
            if _unknown_method(answer):
                raise ToolError("update the Riko Helper extension to see the "
                                "focused tab")
            raise ToolError(str(answer.get("error") or "the browser tool failed"))
        t = answer.get("result")
        return self._tab_row(t) if isinstance(t, dict) and t else None

    def _show_private(self) -> bool:
        d = (self.cfg.get("desktop") or {}).get("awareness")
        if isinstance(d, dict):
            return bool(d.get("private_tabs", False))
        return False

    # ── the tool surface ───────────────────────────────────────
    def _filter_window(self, w: dict) -> dict | None:
        """Privacy pass: denylisted windows collapse to {app: <hidden>}."""
        if self.awareness.hides(w.get("app", ""), w.get("title", "")):
            return {"app": "<hidden>", "id": "", "title": "",
                    "workspace": w.get("workspace", ""),
                    "monitor": w.get("monitor", ""),
                    "focused": w.get("focused", False)}
        return w

    def desktop_windows(self) -> dict:
        b = self._need_backend()
        wins = [self._filter_window(w) for w in b.windows()]
        return {"backend": b.name, "windows": wins}

    def desktop_focused(self) -> dict:
        b = self._need_backend()
        w = b.focused()
        out = {"backend": b.name, "window": self._filter_window(w) if w else None}
        if w and self.bridge is not None and self.bridge.connected() \
                and FIREFOX_CLASSES.search(w.get("app") or ""):
            shown = self._filter_window(w) or {}
            if shown.get("app") != "<hidden>":
                try:
                    tab = self.active_tab()
                    if tab and self.awareness.hides_private(tab["title"], tab["url"]):
                        tab = {"title": "<hidden>", "url": ""}
                    out["browser_tab"] = tab
                except ToolError as e:
                    out["browser_tab_note"] = str(e)
        return out

    def desktop_monitors(self) -> dict:
        b = self._need_backend()
        out = {"backend": b.name, "monitors": b.monitors()}
        configured = getattr(b, "configured_monitors", lambda: [])()
        if configured:
            out["configured"] = configured
        return out

    def desktop_apps(self, query: str = "") -> dict:
        rows = self.index.find(str(query or ""), limit=25)
        return {"apps": [{"id": a["id"], "name": a["name"],
                          "generic": a["generic"], "keywords": a["keywords"]}
                         for a in rows]}

    def open_app(self, app: str, monitor=None, workspace=None) -> dict:
        b = self.backend
        return apps_open_app(
            app, monitor=monitor, workspace=workspace,
            backend=b if (b and b.supports_launch) else None,
            index=self.index, preferences=self.preferences(),
            runner=self.runner,
            resolve_monitor=lambda: (b.monitors() if b else []))

    def focus_window(self, window: str) -> dict:
        b = self._need_backend()
        if not hasattr(b, "focus_window"):
            raise ToolError(f"this desktop ({b.name}) cannot focus windows")
        return b.focus_window(window)

    def executors(self) -> dict:
        return {
            "desktop_windows": self.desktop_windows,
            "desktop_focused": self.desktop_focused,
            "desktop_monitors": self.desktop_monitors,
            "desktop_apps": self.desktop_apps,
            "open_app": self.open_app,
            "focus_window": self.focus_window,
            "browser_tabs": self.browser_tabs,
        }


def _unknown_method(answer: dict) -> bool:
    return "unknown method" in str(answer.get("error") or "").lower()
