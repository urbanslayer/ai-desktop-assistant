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
from .browser_windows import (FF_CLASS_RE, match_windows, parse_workspace_ref,
                               split_firefox_title)
from .monitors import monitor_phrase, resolve_monitor_phrase
from .util import clean_title, Steps

FIREFOX_CLASSES = re.compile(r"firefox|thunderbird", re.I)

# tools the owner approved without confirmation (docs/DESKTOP_SENSE.md,
# ff78db1): risk stays `act`, but the hub default permission is `always`.
ALWAYS_PERMISSION_TOOLS = ("open_app", "switch_workspace", "focus_window")


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
                        "Denylisted apps show as '<hidden>'. current_workspaces "
                        "maps each screen phrase to the workspace it shows now "
                        "(e.g. {'middle Dell': '2'}); a workspace with no "
                        "windows simply contributes no rows — that is normal.",
         "parameters": _obj({})},
        {"name": "desktop_focused", "risk": "read",
         "description": "The focused window (same shape as desktop_windows rows) "
                        "plus the Firefox windows: browser_windows lists every "
                        "Firefox window (active tab, tab count, screen, rank — "
                        "index 0 is the CURRENT tab, ordered by real focus "
                        "recency, not Firefox's own order), browser_tab is that "
                        "window's tab and browser_screen its screen phrase. "
                        "When Firefox has windows but is not focused, "
                        "browser_tab_note says so and names the most recent "
                        "window's screen.",
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
                        "left/middle/right; workspace where allowed. "
                        "follow=true ALSO switches the view there (use it when "
                        "the user said 'switch to X and open ...'); without it "
                        "the app opens there without stealing the view. "
                        "No keystrokes needed for any of this.",
         "parameters": _obj({"app": {"type": "string"},
                             "monitor": {"type": "string",
                                         "description": "'left', 'right', "
                                                        "'the Dell on the right', "
                                                        "'the portable screen', or a name"},
                             "workspace": {"type": "string",
                                           "description": "number or name, "
                                                          "'2' or 'the web one'"},
                             "follow": {"type": "boolean",
                                        "description": "default true when a "
                                                       "workspace/screen was "
                                                       "asked for: switch there "
                                                       "too"}}),
         "required": ["app"]},
        {"name": "switch_workspace", "risk": "act", "permission_default": "always",
         "description": "Show a workspace / virtual desktop: '2', 'desktop 2', "
                        "'virtual desktop two' or a workspace name. No "
                        "keystrokes needed — this IS the way. monitor: show it "
                        "on that screen too ('left', 'the portable screen'). "
                        "On success the result says exactly what changed: "
                        "now_showing {workspace, screen, windows_on_it} and a "
                        "message like 'Now showing workspace 2 on the middle "
                        "Dell (empty).'. An empty workspace has no focused "
                        "window; that is success, not a failure.",
         "parameters": _obj({"workspace": {"type": "string"},
                             "monitor": {"type": "string",
                                         "description": "name or phrase; "
                                                        "optional"}}),
         "required": ["workspace"]},
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

    # ── Firefox windows: extension view + compositor cross-reference ──
    def _ext_windows(self) -> list[dict] | None:
        """Per-window view from the Riko Helper: [{window, title, tab,
        tab_count, focused, private}] in the extension's own
        most-recently-focused order. `activeTabs` (extension 0.1.3+) first;
        an older one groups `listTabs` by windowId (one active tab per
        window, focus order unknown). None = the extension knows neither
        (tabs-only era; callers fall back to activeTab/tabs)."""
        answer = self._bridge_call("activeTabs")
        if answer.get("ok"):
            res = answer.get("result") or {}
            fwid = res.get("focusedWindowId")
            rows = []
            for w in res.get("windows") or []:
                tab = w.get("activeTab") or {}
                rows.append({"window": w.get("windowId"),
                             "title": clean_title(tab.get("title")
                                                  or w.get("title") or ""),
                             "tab": self._tab_row(tab) if tab else None,
                             "tab_count": w.get("tabCount"),
                             "focused": bool(w.get("focused"))
                                        or (fwid is not None
                                            and w.get("windowId") == fwid),
                             "private": str(w.get("state") or "").lower()
                                        in ("private", "incognito")})
            return rows
        if not _unknown_method(answer):
            raise ToolError(str(answer.get("error") or "the browser tool failed"))
        answer = self._bridge_call("listTabs")     # pre-0.1.3 fallback
        if answer.get("ok"):
            order, byw = [], {}
            for t in answer.get("result") or []:
                row = self._tab_row(t)
                wid = row["window"]
                if wid not in byw:
                    byw[wid] = {"window": wid, "title": "", "tab": None,
                                "tab_count": 0, "focused": None,
                                "private": False}
                    order.append(wid)
                g = byw[wid]
                g["tab_count"] += 1
                if row["active"] and g["tab"] is None:
                    g["tab"] = row
                    g["title"] = row["title"]
                    g["focused"] = True
                    # the window's privacy follows its ACTIVE tab: old
                    # extensions cannot tell us the window's state, and a
                    # background private tab must not hide a normal window
                    g["private"] = row["private"]
                elif g["tab"] is None:
                    g["private"] = g["private"] or row["private"]
            for wid in order:      # no window-level focus info on old builds
                if byw[wid]["tab"] is None:
                    byw[wid]["focused"] = False
            return [byw[w] for w in order]
        if _unknown_method(answer):
            return None
        raise ToolError(str(answer.get("error") or "the browser tool failed"))

    def _monitors_safe(self) -> list[dict]:
        b = self.backend
        if b is None:
            return []
        try:
            return b.monitors() or []
        except ToolError:
            return []

    def _screen_of(self, monitor_name: str, mons: list[dict]) -> str:
        for m in mons:
            if str(m.get("name") or "") == str(monitor_name or ""):
                return monitor_phrase(m, mons)
        return str(monitor_name or "")

    def _browser_window_rows(self, ext: list[dict] | None = None,
                             allow_compositor: bool = True) -> list[dict]:
        """Firefox windows the model can talk about, ordered so index 0 is
        THE current tab: compositor focus recency (Hyprland
        focusHistoryID / the focused window) first, the extension's own
        order only when no compositor match exists. Privacy-filtered.
        `allow_compositor` False (awareness off — browser_tabs is
        registered even then) keeps screens/workspaces out of the rows."""
        if ext is None:
            ext = self._ext_windows()
        if ext is None:
            # tabs-only extension: one pseudo-window from activeTab,
            # placed on the compositor's focused firefox window if any
            try:
                tab = self.active_tab()
            except ToolError:
                return []
            if tab and self.awareness.hides_private(tab["title"], tab["url"]):
                tab = {"title": "<hidden>", "url": ""}
            comp = None
            b = self.backend if allow_compositor else None
            if b is not None:
                try:
                    wins = b.windows()
                except ToolError:
                    wins = []
                ff = [w for w in wins if w and FF_CLASS_RE.search(w.get("app") or "")
                      and not self.awareness.hides(w.get("app", ""), w.get("title", ""))]
                comp = next((w for w in ff if w.get("focused")), ff[0] if ff else None)
            mons = self._monitors_safe()
            return [{
                "window": None,
                "screen": self._screen_of(comp.get("monitor", "") if comp else "", mons),
                "monitor": str(comp.get("monitor", "")) if comp else "",
                "workspace": str(comp.get("workspace", "")) if comp else "",
                "tab": ({"id": tab.get("id"), "title": tab["title"],
                         "url": tab["url"]} if tab else None),
                "tab_count": None, "focused_app": True, "rank": 0,
                "private": False, "firefox_focused": True,
            }]
        comp_windows = []
        b = self.backend if allow_compositor else None
        if b is not None:
            try:
                for w in b.windows() or []:
                    if not w or not FF_CLASS_RE.search(w.get("app") or ""):
                        continue
                    if self.awareness.hides(w.get("app", ""), w.get("title", "")):
                        # the denylist hides 'Private Browsing' titles, but an
                        # owner who opted into private tabs still wants the
                        # private window's screen/tab
                        _, priv = split_firefox_title(w.get("title", ""))
                        if not (priv and self._show_private()):
                            continue
                    comp_windows.append(w)
            except ToolError:
                comp_windows = []
        mons = self._monitors_safe() if allow_compositor else []
        pairs = match_windows([e for e in ext], comp_windows)
        rows, rank = [], 0
        for e, comp in pairs:
            private = bool(e.get("private") or (e.get("tab") or {}).get("private")
                           or e.get("_comp_private"))
            if private and not self._show_private():
                continue
            tab = None
            if e.get("tab"):
                t = e["tab"]
                if self.awareness.hides_private(t["title"], t["url"]):
                    tab = {"id": t.get("id"), "title": "<hidden>", "url": ""}
                else:
                    tab = {"id": t.get("id"), "title": t["title"], "url": t["url"]}
            mon_name = str(comp.get("monitor", "")) if comp else ""
            rows.append({
                "window": e.get("window"),
                "screen": self._screen_of(mon_name, mons),
                "monitor": mon_name,
                "workspace": str(comp.get("workspace", "")) if comp else "",
                "tab": tab,
                "tab_count": e.get("tab_count"),
                # is this the compositor-focused window? (extension's own
                # flag only when no compositor match exists)
                "focused_app": bool(comp.get("focused")) if comp
                               else bool(e.get("focused")),
                "rank": rank,
                "private": private,
                "firefox_focused": bool(e.get("focused")),
            })
            rank += 1
        return rows

    def browser_tabs(self) -> dict:
        """{"windows": [per-Firefox-window rows, most recently focused
        first — THE current tab is windows[0]], "tabs": [every tab row]}.
        The windows list is the answer to 'what is my current tab' — the
        compositor's focus order decides, not Firefox's own last-focused."""
        answer = self._bridge_call("listTabs")
        tabs: list[dict] = []
        ext = None
        if answer.get("ok"):
            tabs = [self._tab_row(t) for t in answer.get("result") or []]
        elif _unknown_method(answer):
            answer = self._bridge_call("tabs")   # old extensions
            if answer.get("ok"):
                tabs = [self._tab_row({"id": i + 1, "title": r.get("title"),
                                       "url": r.get("url"),
                                       "active": r.get("active")})
                        for i, r in enumerate(answer.get("result") or [])]
            else:
                raise ToolError("the Riko Helper extension is too old for tab "
                                "lists — update the Riko Helper extension")
        else:
            raise ToolError(str(answer.get("error") or "the browser tool failed"))
        # awareness off: browser_tabs still answers (it is a browser tool),
        # but screen/workspace names are desktop awareness data -> stripped
        windows = self._browser_window_rows(ext,
                                            allow_compositor=self.awareness.enabled)
        if not windows and tabs:
            # tabs-only extension era: group the flat list by windowId so
            # 'windows' still exists (focus order unknown, compositor wins)
            groups, order = {}, []
            for r in tabs:
                wid = r.get("window")
                if wid not in groups:
                    groups[wid] = {"window": wid, "title": "", "tab": None,
                                   "tab_count": 0, "focused": False,
                                   "private": False}
                    order.append(wid)
                g = groups[wid]
                g["tab_count"] += 1
                g["private"] = g["private"] or r["private"]
                if r["active"] and g["tab"] is None:
                    g["tab"], g["title"], g["focused"] = r, r["title"], True
            windows = self._browser_window_rows(
                [groups[w] for w in order],
                allow_compositor=self.awareness.enabled)
        tabs = [r for r in tabs if not (r["private"] and not self._show_private())]
        return {"windows": windows, "tabs": tabs}

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
        out = {"backend": b.name, "windows": wins}
        # What each screen shows RIGHT NOW (2026-11-06: after a switch the
        # model re-reads windows and mistook an empty workspace for failure;
        # this top-level map shows the change without any interpretation).
        try:
            mons = b.monitors()
        except ToolError:
            mons = []
        current = {}
        for m in mons:
            aw = str(m.get("active_workspace") or "")
            if aw:
                current[monitor_phrase(m, mons) or str(m.get("name") or "?")] = aw
        if current:
            out["current_workspaces"] = current
        return out

    def desktop_focused(self) -> dict:
        b = self._need_backend()
        w = b.focused()
        shown = self._filter_window(w) if w else None
        if shown is not None and shown.get("app") == "<hidden>":
            shown = None      # denylisted: dropped from the focused answer
        out = {"backend": b.name, "window": shown}
        if shown is None:
            out["note"] = ("no window is focused right now — normal on an "
                           "empty workspace, not a failed switch")
        if self.bridge is None or not self.bridge.connected():
            return out
        ff_shown = bool(shown and FIREFOX_CLASSES.search(shown.get("app") or ""))
        try:
            any_ff = any(FIREFOX_CLASSES.search(x.get("app") or "")
                         for x in b.windows() if x and x.get("app"))
        except ToolError:
            any_ff = ff_shown
        if not (ff_shown or any_ff):
            return out
        try:
            rows = self._browser_window_rows()
        except ToolError as e:
            if ff_shown:
                out["browser_tab_note"] = str(e)
            return out
        if not rows:
            return out
        out["browser_windows"] = rows
        top = next((r for r in rows if r.get("focused_app")), rows[0])
        if ff_shown:
            out["browser_tab"] = top["tab"]
            out["browser_screen"] = top["screen"]
        else:
            where = top.get("screen") or "another screen"
            title = (top.get("tab") or {}).get("title") or "its own tab"
            out["browser_tab_note"] = (f"Firefox isn't focused; the most recent "
                                       f"Firefox window is on {where}: {title}")
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

    def open_app(self, app: str, monitor=None, workspace=None,
                 follow=None) -> dict:
        b = self.backend
        if workspace not in (None, ""):
            # 'virtual desktop two' -> '2' before the compositor sees it
            workspace = parse_workspace_ref(workspace) or str(workspace).strip()
        return apps_open_app(
            app, monitor=monitor, workspace=workspace, follow=follow,
            backend=b if (b and (getattr(b, "supports_launch", False)
                                 or hasattr(b, "switch_workspace"))) else None,
            index=self.index, preferences=self.preferences(),
            runner=self.runner,
            resolve_monitor=lambda: (b.monitors() if b else []))

    def switch_workspace(self, workspace: str, monitor=None, steps=None) -> dict:
        own = steps is None
        if own:
            steps = Steps(f"switch_workspace {str(workspace or '').strip()}")
        ok = False
        try:
            b = self._need_backend()
            if not hasattr(b, "switch_workspace"):
                raise ToolError(f"this desktop ({b.name}) cannot switch workspaces")
            ref = parse_workspace_ref(workspace)
            if ref is None:
                raise ToolError("switch_workspace: give a workspace number, name or "
                                "phrase ('2', 'desktop 2', 'virtual desktop two')")
            mon_name = None
            if monitor:
                resolved, err = resolve_monitor_phrase(monitor, self._monitors_safe(),
                                                       backend_name=b.name)
                if err:
                    raise ToolError(err)
                mon_name = str(resolved.get("name") or "")
                steps.mark("monitor-phrase")
            import inspect
            if "steps" in inspect.signature(b.switch_workspace).parameters:
                out = b.switch_workspace(ref, monitor=mon_name, steps=steps)
            else:
                out = b.switch_workspace(ref, monitor=mon_name)
            ok = True
            return out
        finally:
            if own:
                steps.finish("ok" if ok else "ERROR (see raised error)")

    def focus_window(self, window: str) -> dict:
        b = self._need_backend()
        if not hasattr(b, "focus_window"):
            raise ToolError(f"this desktop ({b.name}) cannot focus windows")
        return b.focus_window(window)

    def executors(self) -> dict:
        raw = {
            "desktop_windows": self.desktop_windows,
            "desktop_focused": self.desktop_focused,
            "desktop_monitors": self.desktop_monitors,
            "desktop_apps": self.desktop_apps,
            "open_app": self.open_app,
            "switch_workspace": self.switch_workspace,
            "focus_window": self.focus_window,
            "browser_tabs": self.browser_tabs,
        }
        return {name: self._timed(name, fn) for name, fn in raw.items()}

    def _timed(self, name, fn):
        """ONE '[desktop] <action>: ... -> ok' timing line per call (the
        14 s switch of 2026-11-06 was untraceable without these). Executors
        that accept `steps` decorate their line with named sub-steps."""
        import inspect
        takes_steps = "steps" in inspect.signature(fn).parameters

        def call(**kw):
            st = Steps(name)
            try:
                out = fn(steps=st, **kw) if takes_steps else fn(**kw)
            except Exception as e:
                st.finish("ERROR: " + " ".join(str(e).split())[:160])
                raise
            st.finish("ok")
            return out
        call.__name__ = str(name)
        return call


def _unknown_method(answer: dict) -> bool:
    return "unknown method" in str(answer.get("error") or "").lower()
