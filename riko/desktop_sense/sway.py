"""Sway backend: `swaymsg -t get_tree` for state, sway commands for actions.

Tree nodes nest under "nodes" (tiled) and "floating_nodes"; window leaves
have type "con"/"floating_con" with pid/app_id/name/focused. The containing
output and workspace come from ancestors — we walk with that context.
"""
from __future__ import annotations

import json
import os
import shutil
import time

from .api_errors import ToolError
from .util import clean_title, run_json


def _env_ready(env=None):
    e = env if env is not None else os.environ
    return bool(e.get("SWAYSOCK")) and bool(shutil.which("swaymsg"))


def _walk(node, output="", workspace="", depth=0):
    """Yield (node, output, workspace) for every node in the tree."""
    if not isinstance(node, dict):
        return
    ntype = node.get("type")
    out = node.get("name", output) if ntype == "output" else output
    ws = node.get("name", workspace) if ntype in ("workspace",) else workspace
    yield node, out, ws
    for key in ("nodes", "floating_nodes"):
        for child in node.get(key) or []:
            yield from _walk(child, out, ws, depth + 1)


class SwayBackend:
    name = "sway"
    supports_launch = True

    def __init__(self, runner=None, env=None):
        self.runner = runner
        self.env = env if env is not None else os.environ

    def _tree(self):
        return run_json(["swaymsg", "-t", "get_tree"], timeout=10,
                        runner=self.runner, error="swaymsg get_tree failed")

    def _command(self, cmd, runner=None):
        from .util import run
        done = run(["swaymsg", "-t", "command", cmd], timeout=10,
                   runner=runner if runner is not None else self.runner)
        return getattr(done, "returncode", 0) == 0

    # ── state ──────────────────────────────────────────────────
    def monitors(self) -> list[dict]:
        tree = self._tree()
        out = []
        for node, _, _ in _walk(tree):
            if node.get("type") != "output":
                continue
            rect = node.get("rect") or {}
            # sway puts modes on the output node only in newer versions;
            # fall back to the current rect
            modes = node.get("modes") or []
            current = next((m for m in modes if isinstance(m, dict)
                            and m.get("width")), {})
            out.append({
                "name": node.get("name", ""),
                "description": node.get("description", ""),
                "size": "{}x{}".format(
                    current.get("width", rect.get("width")),
                    current.get("height", rect.get("height"))),
                "position": f"{rect.get('x', 0)}x{rect.get('y', 0)}",
                "scale": node.get("scale", 1),
                "focused": bool(node.get("focused")),
                "active_workspace": next(
                    (c.get("name", "") for c in (node.get("nodes") or [])
                     if isinstance(c, dict) and c.get("type") == "workspace"
                     and c.get("focused")), ""),
                "disabled": bool(node.get("layout") == "disabled"),
            })
        return out

    def _window_nodes(self):
        tree = self._tree()
        wins = []
        for node, output, ws in _walk(tree):
            if node.get("type") not in ("con", "floating_con") or \
                    not node.get("pid") or node.get("nodes") or node.get("layout") in \
                    ("splith", "splitv", "stacked", "tabbed", "output", "root", "dockarea"):
                continue
            # leaves only: real windows have no child containers and a pid
            wins.append((node, output, ws))
        return wins

    def windows(self) -> list[dict]:
        out = []
        for node, output, ws in self._window_nodes():
            out.append({
                "id": str(node.get("id", "")),
                "app": node.get("app_id") or node.get("window_properties", {}).get("class", ""),
                "title": clean_title(node.get("name", "")),
                "workspace": ws,
                "monitor": output,
                "focused": bool(node.get("focused")),
                "fullscreen": node.get("fullscreen_mode", 0) != 0,
                "floating": node.get("type") == "floating_con"
                            or node.get("layout") == "floating",
                "pid": node.get("pid"),
            })
        return out

    def focused(self) -> dict | None:
        for w in self.windows():
            if w["focused"]:
                return w
        return None

    # ── actions ────────────────────────────────────────────────
    def launch(self, argv, monitor=None, workspace=None, runner=None, env=None):
        before = {w["id"] for w in self.windows()}
        # sway's `exec` runs its argument through sh — our argv is already
        # sanitized (desktop Exec words, no model strings); quote defensively
        quoted = " ".join("'" + str(a).replace("'", "'\\''") + "'" for a in argv)
        if not self._command(f"exec {quoted}", runner=runner):
            raise ToolError("swaymsg exec failed")
        moved = {}
        hint = str(argv[0]).rsplit("/", 1)[-1].lower()
        deadline = time.monotonic() + 4.0
        new_win = None
        while time.monotonic() < deadline and not new_win:
            try:
                cur = [w for w in self.windows() if w["id"] not in before]
            except ToolError:
                cur = []
            match = [w for w in cur if hint and hint in (w["app"] or "").lower()
                     or (w["app"] or "").lower() in hint]
            new_win = (match or cur or [None])[-1]
            if not new_win:
                time.sleep(0.25)
        if new_win and (monitor or workspace not in (None, "")):
            if workspace not in (None, ""):
                if self._command(f'[con_id="{new_win["id"]}"] '
                                 f'move container to workspace {workspace}',
                                 runner=runner):
                    moved = {"workspace": str(workspace)}
            if monitor and "workspace" not in moved:
                if self._command(f'[con_id="{new_win["id"]}"] '
                                 f'move container to output {monitor}',
                                 runner=runner):
                    moved = {"monitor": monitor}
                else:
                    moved = {"note": "launched but could not move it to the screen"}
        return moved or {"launched": True}

    def focus_window(self, ref: str) -> dict:
        ref_l = str(ref or "").strip().lower()
        wins = self.windows()
        win = next((w for w in wins if str(w.get("id", "")).lower() == ref_l), None)
        if win is None:
            matches = [w for w in wins
                       if ref_l in (w.get("title") or "").lower()
                       or ref_l == (w.get("app") or "").lower()]
            win = matches[0] if len(matches) == 1 else (matches or [None])[0]
        if win is None:
            raise ToolError(f"no open window matches {ref!r} "
                            "(use a window id, app id or title from desktop_windows)")
        if not self._command(f'[con_id="{win["id"]}"] focus'):
            raise ToolError("swaymsg focus failed")
        return {"focused": win.get("app") or ref, "title": win.get("title", "")}
