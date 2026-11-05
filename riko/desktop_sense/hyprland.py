"""Hyprland backend: hyprctl -j for state, `hyprctl dispatch` for actions,
plus the read-only Lua config reader (hyprlua)."""
from __future__ import annotations

import os
import shutil
import time

from .api_errors import ToolError
from .hyprlua import HyprLua
from .util import clean_title, run_json


def _env_ready(env=None):
    e = env if env is not None else os.environ
    return bool(e.get("HYPRLAND_INSTANCE_SIGNATURE")) and bool(shutil.which("hyprctl"))


class HyprlandBackend:
    name = "hyprland"
    supports_launch = True

    def __init__(self, runner=None, config_dir=None, env=None, lua=None):
        self.runner = runner
        self.env = env if env is not None else os.environ
        self._lua = lua  # HyprLua or None (lazily read from the real dir)
        self._lua_tried = lua is not None
        self._config_dir = config_dir

    # ── raw hyprctl ────────────────────────────────────────────
    def _hyprctl(self, *args, error="hyprctl failed"):
        return run_json(["hyprctl", *args], timeout=10, runner=self.runner,
                        error=error)

    # ── Lua config (read-only; owner instruction) ──────────────
    def lua(self):
        if not self._lua_tried:
            self._lua_tried = True
            try:
                self._lua = HyprLua(self._config_dir)
            except Exception:
                self._lua = None
        return self._lua

    def preferences(self) -> dict:
        lua = self.lua()
        return dict(lua.preferred) if lua else {}

    def configured_monitors(self) -> list[dict]:
        lua = self.lua()
        return lua.configured_monitors() if lua else []

    # ── state ──────────────────────────────────────────────────
    def monitors(self) -> list[dict]:
        mons = self._hyprctl("monitors", "-j", error="hyprctl monitors failed")
        out = []
        for m in mons if isinstance(mons, list) else []:
            out.append({
                "id": m.get("id"),
                "name": m.get("name", ""),
                "description": m.get("description") or " ".join(
                    x for x in (m.get("make", ""), m.get("model", "")) if x).strip(),
                "size": "{}x{}".format(
                    (m.get("size") or {}).get("x") if isinstance(m.get("size"), dict)
                    else (m.get("recommendedMode") or {}).get("width"),
                    (m.get("size") or {}).get("y") if isinstance(m.get("size"), dict)
                    else (m.get("recommendedMode") or {}).get("height")),
                "position": f"{m.get('x', 0)}x{m.get('y', 0)}",
                "scale": m.get("scale", 1),
                "focused": bool(m.get("focused", False)),
                "active_workspace": (m.get("activeWorkspace") or {}).get("name", ""),
                "disabled": bool(m.get("disabled", False)),
            })
        return out

    def _safe_workspaces(self):
        try:
            ws = self._hyprctl("workspaces", "-j", error="workspaces failed")
        except ToolError:
            return []
        # hyprctl also lists the special (scratchpad) workspaces; skip them
        return [w for w in (ws if isinstance(ws, list) else [])
                if not str(w.get("name", "")).startswith("special:")]

    def windows(self) -> list[dict]:
        clients = self._hyprctl("clients", "-j", error="hyprctl clients failed")
        focused_addr = ""
        try:
            focused_addr = (self._hyprctl("activewindow", "-j",
                                          error="activewindow failed") or {}).get("address", "")
        except ToolError:
            pass
        try:
            mon_names = {m["id"]: m["name"] for m in self.monitors()}
        except ToolError:
            mon_names = {}
        safe = {str(w.get("name", "")) for w in self._safe_workspaces()}
        out = []
        for c in clients if isinstance(clients, list) else []:
            ws_name = (c.get("workspace") or {}).get("name", "")
            if ws_name.startswith("special:") or (safe and ws_name and
                                                  ws_name not in safe):
                continue        # scratchpad/special workspaces are not exposed
            mid = c.get("monitor")
            mon = mon_names.get(mid, "" if mid is None else str(mid))
            fr = c.get("focusHistoryID")
            out.append({
                "id": c.get("address", ""),
                "app": c.get("class", ""),
                "title": clean_title(c.get("title", "")),
                "workspace": ws_name,
                "monitor": mon or str(mid),
                "focused": c.get("address") == focused_addr,
                # 0 = currently focused, larger = less recently focused
                # (docs/DESKTOP_SENSE.md multi-window 'current tab')
                "focus_recency": fr if isinstance(fr, int) else None,
                "fullscreen": bool(c.get("fullscreen")),
                "floating": bool(c.get("floating")),
                "pid": c.get("pid"),
            })
        return out

    def focused(self) -> dict | None:
        try:
            w = self._hyprctl("activewindow", "-j", error="activewindow failed")
        except ToolError:
            return None
        if not isinstance(w, dict) or not w:
            return None
        return {
            "id": w.get("address", ""),
            "app": w.get("class", ""),
            "title": clean_title(w.get("title", "")),
            "workspace": (w.get("workspace") or {}).get("name", ""),
            "monitor": str(w.get("monitor", "")),
            "focused": True,
            "focus_recency": 0,        # activewindow is history entry 0
            "fullscreen": bool(w.get("fullscreen")),
            "floating": bool((w.get("floating") or {}).get("state")
                             if isinstance(w.get("floating"), dict)
                             else w.get("floating")),
            "pid": w.get("pid"),
        }

    # ── actions ────────────────────────────────────────────────
    def dispatch(self, expr, runner=None):
        from .util import run
        done = run(["hyprctl", "dispatch", expr], timeout=10,
                   runner=runner if runner is not None else self.runner)
        return getattr(done, "returncode", 0) == 0

    def _find_new_window(self, before_ids, hint_app, timeout=4.0):
        """Poll clients for a window that appeared after `before_ids`."""
        deadline = time.monotonic() + timeout
        best = None
        while time.monotonic() < deadline:
            try:
                cur = {w["id"]: w for w in self.windows()}
            except ToolError:
                cur = {}
            new = [w for wid, w in cur.items() if wid not in before_ids]
            if hint_app:
                match = [w for w in new
                         if hint_app.lower().split("-")[0]
                         and hint_app.lower() in (w["app"] or "").lower()
                         or (w["app"] or "").lower() in hint_app.lower()]
                if match:
                    return match[-1]
            if new:
                best = new[-1]
                # give a matching window a moment longer to show up
                if time.monotonic() > deadline - timeout + 1.2:
                    return best
            time.sleep(0.25)
        return best

    def launch(self, argv, monitor=None, workspace=None, runner=None, env=None,
               follow=False):
        """`hyprctl dispatch exec <cmd>`; when a target monitor/workspace is
        asked for, move the new window afterwards (silent, no focus steal
        unless we moved it at all). `follow=True` (open_app's follow flag)
        switches to where the app lands: no `silent` in the workspace tag
        and movetoworkspace instead of movetoworkspacesilent."""
        cmd = " ".join(str(a) for a in argv)          # hyprctl exec takes one string
        ws_tag = ""
        if workspace not in (None, ""):
            ws_tag = (f"[workspace {workspace}] " if follow
                      else f"[workspace {workspace} silent] ")
        before = {w["id"] for w in self.windows()}
        if not self.dispatch(f'exec "{ws_tag}{cmd}"', runner=runner):
            raise ToolError("hyprctl dispatch exec failed")
        moved = {}
        hint = str(argv[0]).rsplit("/", 1)[-1]
        new_win = self._find_new_window(before, hint)
        if new_win and (monitor or workspace not in (None, "")):
            target_ws = str(workspace) if workspace not in (None, "") else None
            if monitor:
                # land on the monitor's current workspace (rules move it there)
                mons = {m["name"]: m for m in self.monitors()}
                m = mons.get(monitor)
                if m is None:
                    raise ToolError(f"no such monitor: {monitor}")
                target_ws = target_ws or str(m.get("active_workspace") or "")
            if target_ws:
                mover = "movetoworkspace" if follow else "movetoworkspacesilent"
                if self.dispatch(f"{mover} {target_ws},address:{new_win['id']}",
                                 runner=runner):
                    moved = {"workspace": target_ws}
                else:
                    moved = {"note": "launched but could not move it to the screen"}
            if monitor and not moved.get("note"):
                # keep that workspace ON the requested screen (owner:
                # 'show workspace 2 on the left screen')
                self.dispatch(f"moveworkspacetomonitor {target_ws} {monitor}",
                              runner=runner)
                moved["monitor"] = monitor
        return moved or {"launched": True}

    def switch_workspace(self, workspace, monitor=None, runner=None) -> dict:
        """Show a workspace/virtual desktop (number or name). With a
        monitor: moveworkspacetomonitor first — that is the dispatch that
        matches 'show workspace 2 on the left screen'."""
        ref = str(workspace or "").strip()
        if not ref:
            raise ToolError("switch_workspace: give a workspace number or name")
        if monitor:
            if not self.dispatch(f"moveworkspacetomonitor {ref} {monitor}",
                                 runner=runner):
                raise ToolError("hyprctl moveworkspacetomonitor failed")
        if not self.dispatch(f"workspace {ref}", runner=runner):
            raise ToolError("hyprctl workspace switch failed")
        return {"workspace": ref, "monitor": monitor or ""}

    def focus_window(self, ref: str) -> dict:
        ref = str(ref or "").strip()
        win = self._find_window(ref)
        if win is None:
            raise ToolError(f"no open window matches {ref!r} "
                            "(use a window id, app class or title from desktop_windows)")
        if not self.dispatch(f"focuswindow address:{win['id']}"):
            raise ToolError("hyprctl focuswindow failed")
        return {"focused": win.get("app") or ref, "title": win.get("title", "")}

    def _find_window(self, ref):
        ref_l = ref.lower().removeprefix("address:")
        try:
            wins = self.windows()
        except ToolError:
            return None
        for w in wins:
            if str(w.get("id", "")).lower() == ref_l:
                return w
        matches = [w for w in wins
                   if ref_l in (w.get("title") or "").lower()
                   or ref_l == (w.get("app") or "").lower()]
        return matches[0] if len(matches) == 1 else (matches or [None])[0]
