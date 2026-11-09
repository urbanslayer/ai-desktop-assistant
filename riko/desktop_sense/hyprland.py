"""Hyprland backend: hyprctl -j for state, `hyprctl dispatch` for actions,
plus the read-only Lua config reader (hyprlua).

Two config styles (verified in the Hyprland v0.56.2 source):

* **Lua config** (owner's setup): `hyprctl dispatch <x>` is literally
  `eval("return hl.dispatch(<x>)")` (src/debug/HyprCtl.cpp, dispatchRequest
  ~lines 1126-1141). Classic dispatcher strings ('workspace 6', 'focuswindow
  address:0x..') are invalid Lua: hyprctl prints an error plus the note
  "dispatch in lua is a shorthand for hl.dispatch(...)" — and can still exit
  0. Actions must therefore be hl.dsp.* expressions and success is ONLY the
  exact reply 'ok'.
* **Legacy hyprlang config**: classic dispatcher strings; reply 'ok' on
  success.

Lua action expressions used here (src/config/lua/bindings/):

* workspace switch (creates empty workspaces exactly like the owner's
  Super+N binds): ``hl.dsp.focus({ workspace = 6 })`` — numbers unquoted,
  names quoted (hlFocus → dsp_changeWorkspace →
  Actions::changeWorkspace → resolveWorkspaceForChange CREATES the workspace
  when it does not exist yet, ConfigActions.cpp ~1040-1044).
* focus window: ``hl.dsp.focus({ window = "address:0x..." })`` — the view
  selector understands address:/class:/title: (src/desktop/state/
  ViewQuery.cpp bySelector ~line 140).
* move window to workspace: ``hl.dsp.window.move({ workspace = 2,
  window = "address:0x...", follow = true|false })`` (hlWindowMove: the
  optional `window` key is the same selector, `follow = false` = silent,
  i.e. movetoworkspacesilent).
* move workspace to monitor: ``hl.dsp.workspace.move({ workspace = 2,
  monitor = "DP-1" })`` (hlWorkspaceMove); focus a monitor:
  ``hl.dsp.focus({ monitor = "DP-1" })``.
* launch: ``hl.dsp.exec_cmd("dolphin", { workspace = "2" })`` — the table is
  a window rule applied to the spawned process (buildRuleFromTable; the
  `workspace`/`monitor` effect names are in WINDOW_RULE_EFFECT_DESCS,
  LuaBindingsInternal.hpp). NOT exec_raw: dsp_execRaw spawns a raw process
  (executor()->spawnRaw), it does not run classic dispatchers.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .api_errors import ToolError
from .hyprlua import HyprLua
from .monitors import monitor_phrase
from .util import clean_title, norm_window_id, run_json, Steps

# probe dispatch: a valid Lua dispatcher call, and the one string hyprlang
# can only ever answer with 'Invalid dispatcher' (hl.dsp.no_op exists in
# registerDispatcherBindings, LuaBindingsDispatchers.cpp)
_LUA_PROBE = "hl.dsp.no_op()"
_LUA_NOTE = "hl.dispatch"      # marker of the Lua-config error note
_WS_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 :._+\-]*")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


def lua_string(value) -> str:
    """A Lua string literal. Values are sanitized upstream (desktop Exec,
    validated workspace refs, hyprctl addresses), but a stray quote or
    backslash must never break out of the dispatch expression."""
    out = ['"']
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\%d" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _env_ready(env=None):
    e = env if env is not None else os.environ
    return bool(e.get("HYPRLAND_INSTANCE_SIGNATURE")) and bool(shutil.which("hyprctl"))


class HyprlandBackend:
    name = "hyprland"
    supports_launch = True

    def __init__(self, runner=None, config_dir=None, env=None, lua=None,
                 config_style=None):
        self.runner = runner
        self.env = env if env is not None else os.environ
        self._lua = lua  # HyprLua or None (lazily read from the real dir)
        self._lua_tried = lua is not None
        self._config_dir = config_dir
        # 'lua' | 'legacy' | None (None = detect on first action, then cache)
        self._config_style = config_style
        # how the style was decided ('file' | 'probe' | ''), for the timing line
        self._style_source = "file" if config_style else ""

    # ── raw hyprctl ────────────────────────────────────────────
    def _hyprctl(self, *args, error="hyprctl failed"):
        return run_json(["hyprctl", *args], timeout=10, runner=self.runner,
                        error=error)

    # ── config style (Lua vs classic hyprlang), cached ─────────
    def config_style(self, runner=None, steps=None) -> str:
        if self._config_style is None:
            self._config_style = self._detect_config_style(runner)
            # 'file' costs zero round trips; only 'probe' can ever be the
            # slow hop (one full hyprctl dispatch through the compositor).
            if steps is not None:
                steps.note(f"style {self._config_style} via {self._style_source}")
        elif steps is not None:
            steps.note("style cached")
        return self._config_style

    def _detect_config_style(self, runner=None) -> str:
        """Decide without the compositor when the config file layout already
        says: 0.55+ uses `hyprland.lua` as the entrypoint, so that file alone
        means a Lua config and plain `hyprland.conf` alone means hyprlang.
        Only when BOTH files exist (or the layout is unreadable) do we pay
        one probe dispatch: `hyprctl dispatch hl.dsp.no_op()` evals to a real
        dispatcher and answers 'ok' on Lua; hyprlang answers
        'Invalid dispatcher'. The probe dispatch can stall seconds behind
        the compositor's main loop, so the file layout wins whenever it is
        unambiguous (2026-11-06: a probe + the real dispatch on the first
        switch after restart were the front half of a 14 s tool call)."""
        try:
            base = Path(self._config_dir or (Path.home() / ".config" / "hypr"))
            lua_file = (base / "hyprland.lua").is_file()
            conf_file = (base / "hyprland.conf").is_file()
        except OSError:
            lua_file = conf_file = False
        if lua_file and not conf_file:
            self._style_source = "file"
            return "lua"
        if conf_file and not lua_file:
            self._style_source = "file"
            return "legacy"
        self._style_source = "probe"
        try:
            reply = self._dispatch_raw(_LUA_PROBE, runner)
        except ToolError:
            reply = ""
        if reply == "ok":
            return "lua"
        if _LUA_NOTE in reply:                    # 'shorthand for hl.dispatch'
            return "lua"
        if "invalid dispatcher" in reply.lower():
            return "legacy"
        return "legacy" if not lua_file else "lua"

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
            focused_addr = norm_window_id((self._hyprctl("activewindow", "-j",
                                           error="activewindow failed") or {})
                                          .get("address", ""))
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
                # canonical address (lowercase hex, 0x): typing keys per-window
                # state off this id and must not lose it over format drift
                "id": norm_window_id(c.get("address", "")),
                "app": c.get("class", ""),
                "title": clean_title(c.get("title", "")),
                "workspace": ws_name,
                "monitor": mon or str(mid),
                "focused": norm_window_id(c.get("address", "")) == focused_addr,
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
            "id": norm_window_id(w.get("address", "")),
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

    def _active_workspace(self):
        try:
            w = self._hyprctl("activeworkspace", "-j",
                              error="activeworkspace failed")
        except ToolError:
            return None
        return w if isinstance(w, dict) and w else None

    def _observed_state(self):
        """The workspace the view actually shows: {id, name, monitor(name)},
        or None when it cannot be read (then the dispatch reply is the only
        evidence we have)."""
        w = self._active_workspace()
        if w is None:
            return None
        mon = w.get("monitor")
        mon_name = ""
        if mon is not None:
            try:
                mon_name = {m["id"]: m["name"] for m in self.monitors()}.get(mon, str(mon))
            except ToolError:
                mon_name = str(mon)
        return {"id": w.get("id"), "name": str(w.get("name") or ""),
                "monitor": mon_name}

    @staticmethod
    def _ws_matches(obs, ref) -> bool:
        """Does the observed active workspace satisfy the requested ref
        ('6', 'code', 'name:code', '2:code')? Unobservable state counts as a
        match — the accepted 'ok' reply stands on its own."""
        if obs is None:
            return True
        s = str(ref).strip()
        if s.lower().startswith("name:"):
            tid, tname = None, s[5:]
        elif ":" in s:
            head, _, tname = s.partition(":")
            tid = int(head) if head.isdigit() else None
        else:
            tid, tname = (int(s), None) if s.isdigit() else (None, s)
        if tid is not None and obs.get("id") == tid:
            return True
        obs_name = str(obs.get("name") or "")
        return bool(tname) and (obs_name == tname
                                or obs_name.rsplit(":", 1)[-1] == tname)

    # ── Lua expression helpers ─────────────────────────────────
    @staticmethod
    def _lua_ws(ref) -> str:
        s = str(ref).strip()
        if s.isdigit():
            # unquoted number: the same selector the owner's Super+N binds
            # use; the switch creates the workspace when it does not exist
            return s
        if _WS_REF_RE.fullmatch(s):
            return lua_string(s)
        raise ToolError(f"unsupported workspace reference: {s!r}")

    @staticmethod
    def _lua_win(win_id) -> str:
        s = norm_window_id(win_id)          # also accepts 0x-less addresses
        if not _ADDR_RE.fullmatch(s):
            raise ToolError(f"invalid window address: {s!r}")
        return lua_string(f"address:{s}")

    # ── actions ────────────────────────────────────────────────
    def _dispatch_raw(self, expr, runner=None) -> str:
        """Run `hyprctl dispatch <expr>` (one argv element — the expression
        never goes through a shell) and return the stripped reply text."""
        from .util import run
        try:
            done = run(["hyprctl", "dispatch", expr], timeout=10,
                       runner=runner if runner is not None else self.runner)
        except FileNotFoundError:
            raise ToolError("hyprctl is not installed") from None
        except subprocess.TimeoutExpired:
            raise ToolError("hyprctl dispatch timed out") from None
        rc = getattr(done, "returncode", 0)
        text = (getattr(done, "stdout", "") or "").strip()
        if rc != 0:
            reason = (getattr(done, "stderr", "") or "").strip() or text \
                or f"exit code {rc}"
            raise ToolError(f"hyprctl dispatch failed: {reason[:300]}")
        return text

    def dispatch(self, expr, runner=None, what="dispatch") -> str:
        """Dispatch and return the reply on success.

        Exit code 0 is NOT success: with a Lua config the reply carries the
        Lua error text (plus the 'shorthand for hl.dispatch' note) while
        hyprctl still exits 0. Success is exactly 'ok'; anything else is
        raised as a ToolError with the real reason."""
        text = self._dispatch_raw(expr, runner)
        if text == "ok":
            return text
        raise ToolError(f"hyprland {what} failed: {text or 'empty reply'}"[:400])

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
        """Launch a command; when a target monitor/workspace is asked for,
        move the new window afterwards (silent unless `follow=True`, in which
        case the view follows the app). Lua configs use
        `hl.dsp.exec_cmd(cmd[, { workspace = "N" }])` — the table is a window
        rule for the spawned process — plus
        `hl.dsp.window.move({ workspace = N, window = "address:..",
        follow = true|false })`; legacy configs keep the classic
        `exec "[workspace N silent] cmd"` / `movetoworkspace*` strings.
        The move is verified against the re-read window list."""
        cmd = " ".join(str(a) for a in argv)
        lua = self.config_style(runner) == "lua"
        has_ws = workspace not in (None, "")
        if lua:
            if has_ws:
                expr = (f"hl.dsp.exec_cmd({lua_string(cmd)}, "
                        f"{{ workspace = {lua_string(str(workspace))} }})")
            else:
                expr = f"hl.dsp.exec_cmd({lua_string(cmd)})"
        else:
            ws_tag = ""
            if has_ws:
                ws_tag = (f"[workspace {workspace}] " if follow
                          else f"[workspace {workspace} silent] ")
            expr = f'exec "{ws_tag}{cmd}"'
        before = {w["id"] for w in self.windows()}
        self.dispatch(expr, runner, what="launch")
        moved = {}
        target_ws = None
        hint = str(argv[0]).rsplit("/", 1)[-1]
        new_win = self._find_new_window(before, hint)
        if new_win and (monitor or has_ws):
            target_ws = str(workspace) if has_ws else None
            if monitor:
                # land on the monitor's current workspace (rules move it there)
                mons = {m["name"]: m for m in self.monitors()}
                m = mons.get(monitor)
                if m is None:
                    raise ToolError(f"no such monitor: {monitor}")
                target_ws = target_ws or str(m.get("active_workspace") or "")
            if target_ws:
                try:
                    if lua:
                        self.dispatch(
                            f"hl.dsp.window.move({{ workspace = "
                            f"{self._lua_ws(target_ws)}, window = "
                            f"{self._lua_win(new_win['id'])}, "
                            f"follow = {'true' if follow else 'false'} }})",
                            runner, what="movetoworkspace")
                    else:
                        mover = "movetoworkspace" if follow else "movetoworkspacesilent"
                        self.dispatch(f"{mover} {target_ws},address:{new_win['id']}",
                                      runner, what=mover)
                    moved = {"workspace": target_ws}
                except ToolError:
                    moved = {"note": "launched but could not move it to the screen"}
            if monitor and not moved.get("note"):
                # keep that workspace ON the requested screen (owner:
                # 'show workspace 2 on the left screen')
                try:
                    if lua:
                        wsel = (f"workspace = {self._lua_ws(target_ws)}, "
                                if target_ws else "")
                        self.dispatch(
                            f"hl.dsp.workspace.move({{ {wsel}"
                            f"monitor = {lua_string(monitor)} }})",
                            runner, what="moveworkspacetomonitor")
                    else:
                        self.dispatch(f"moveworkspacetomonitor {target_ws} {monitor}",
                                      runner, what="moveworkspacetomonitor")
                    moved["monitor"] = monitor
                except ToolError:
                    moved["note"] = ("moved it, but that workspace could not "
                                     "be kept on that screen")
        if moved.get("workspace") and new_win:
            # verify the window actually landed where we said
            try:
                cur = {w["id"]: w for w in self.windows()}.get(new_win["id"])
            except ToolError:
                cur = None
            if cur is not None:
                obs = str(cur.get("workspace") or "")
                want = str(target_ws or "")
                if obs and obs != want and obs.rsplit(":", 1)[-1] != want:
                    moved["note"] = (moved.get("note", "") +
                                     f" window ended on workspace {obs}, "
                                     f"not {want}").strip()
        return moved or {"launched": True}

    def switch_workspace(self, workspace, monitor=None, runner=None,
                         steps=None) -> dict:
        """Show a workspace/virtual desktop (number or name). With a
        monitor: move the workspace onto that screen first — the dispatch
        that matches 'show workspace 2 on the left screen'. Lua configs get
        `hl.dsp.workspace.move({ workspace = N, monitor = "M" })` +
        `hl.dsp.focus({ workspace = N })`; the switch creates an empty
        workspace exactly like the owner's Super+N keybinds. The reply is
        checked and the view is re-read: an accepted command that left the
        view on the old workspace is an error, not 'done'.

        `steps` (util.Steps) collects per-step timings; when omitted the
        call logs its own one-line timing entry. Exactly ONE dispatch goes
        to the compositor per switch (plus one move when a monitor was
        named): every dispatch is a synchronous Lua eval on Hyprland's main
        loop and the only step that can stall, so the timing line names it.
        An EMPTY destination workspace is success — no focused window is
        expected there and nothing in the result says otherwise."""
        ref = str(workspace or "").strip()
        if not ref:
            raise ToolError("switch_workspace: give a workspace number or name")
        own_steps = steps is None
        if own_steps:
            steps = Steps(f"switch_workspace {ref}"
                          + (f" on {monitor}" if monitor else ""))
        ok = False
        try:
            lua = self.config_style(runner, steps=steps) == "lua"
            before = self._active_workspace()
            steps.mark("pre-state")
            if monitor:
                if lua:
                    self.dispatch(f"hl.dsp.workspace.move({{ workspace = "
                                  f"{self._lua_ws(ref)}, "
                                  f"monitor = {lua_string(monitor)} }})",
                                  runner, what="moveworkspacetomonitor")
                else:
                    self.dispatch(f"moveworkspacetomonitor {ref} {monitor}",
                                  runner, what="moveworkspacetomonitor")
                steps.mark("move")
            if lua:
                self.dispatch(f"hl.dsp.focus({{ workspace = {self._lua_ws(ref)} }})",
                              runner, what="workspace switch")
            else:
                self.dispatch(f"workspace {ref}", runner, what="workspace switch")
            steps.mark("dispatch")
            after_w = self._active_workspace()
            try:
                mons = self.monitors()
            except ToolError:
                mons = []
            after = self._observed_from(after_w, mons)
            steps.mark("verify")
            note = ""
            if not self._ws_matches(after, ref):
                ident = str((after or {}).get("name") or (after or {}).get("id") or "")
                same = (before is not None and after_w is not None
                        and (before.get("id"), str(before.get("name") or ""))
                        == (after_w.get("id"), str(after_w.get("name") or "")))
                if same:
                    raise ToolError(
                        "Hyprland accepted the command but the view did not change"
                        f" (still workspace {ident})")
                note = f"switched to workspace {ident}, not {ref}" if ident else ""
            obs_name = (after or {}).get("name") or ""
            obs_id = (after or {}).get("id")
            obs_ws = obs_name or (str(obs_id) if obs_id is not None else ref)
            mon_name = (after or {}).get("monitor") or (monitor or "")
            mon_row = next((m for m in mons if m.get("name") == mon_name), None)
            screen = monitor_phrase(mon_row, mons) if mon_row else mon_name
            n_win = self._windows_on(obs_ws, obs_id)
            if n_win is None:
                tail = ""
            elif n_win == 0:
                tail = " (empty)"
            else:
                tail = f" ({n_win} window{'s' if n_win != 1 else ''})"
            where = f"on the {screen}" if screen else "on the current screen"
            out = {
                "workspace": obs_ws,
                "monitor": mon_name,
                "now_showing": {"workspace": obs_ws, "screen": screen,
                                "windows_on_it": n_win},
                "message": f"Now showing workspace {obs_ws} {where}{tail}.",
                **({"note": note} if note else {}),
            }
            ok = True
            return out
        finally:
            if own_steps:
                steps.finish("ok" if ok else "ERROR (view did not change)")

    def _windows_on(self, ws_name, ws_id):
        """Windows living on the workspace (None when clients cannot be
        read). One hyprctl call; an empty workspace legitimately returns 0."""
        try:
            clients = self._hyprctl("clients", "-j", error="hyprctl clients failed")
        except ToolError:
            return None
        n = 0
        for c in clients if isinstance(clients, list) else []:
            cw = c.get("workspace") or {}
            name = str(cw.get("name") or "")
            if (ws_id is not None and cw.get("id") == ws_id) or (
                    name and (name == str(ws_name)
                              or name.rsplit(":", 1)[-1] == str(ws_name))):
                n += 1
        return n

    @staticmethod
    def _observed_from(w, mons):
        """Map a raw activeworkspace reply (+ monitors rows) to the observed
        view {id, name, monitor(name)}; None when unreadable."""
        if not w:
            return None
        mon = w.get("monitor")
        mon_name = ""
        if mon is not None:
            mon_name = {m["id"]: m["name"] for m in mons}.get(mon, str(mon))
        return {"id": w.get("id"), "name": str(w.get("name") or ""),
                "monitor": mon_name}

    def focus_window(self, ref: str) -> dict:
        ref = str(ref or "").strip()
        win = self._find_window(ref)
        if win is None:
            raise ToolError(f"no open window matches {ref!r} "
                            "(use a window id, app class or title from desktop_windows)")
        lua = self.config_style() == "lua"
        before = self.focused()
        if lua:
            expr = f"hl.dsp.focus({{ window = {self._lua_win(win['id'])} }})"
        else:
            expr = f"focuswindow address:{win['id']}"
        self.dispatch(expr, what="focuswindow")
        after = self.focused()
        out = {"focused": win.get("app") or ref, "title": win.get("title", "")}
        if after:
            out["address"] = after.get("id", "")
            out["workspace"] = after.get("workspace", "")
            if after.get("id") != win.get("id"):
                if before and after.get("id") == before.get("id"):
                    raise ToolError("Hyprland accepted the command but the view "
                                    "did not change (focus stayed where it was)")
                out["note"] = ("focus moved to "
                               f"{after.get('app') or after.get('id') or '?'} instead")
        return out

    def _find_window(self, ref):
        # refs arrive as ids from desktop_windows (canonical already), with
        # or without the 0x prefix, or as an app class / title fragment
        ref_l = norm_window_id(ref)
        try:
            wins = self.windows()
        except ToolError:
            return None
        for w in wins:
            if norm_window_id(w.get("id", "")) == ref_l and ref_l:
                return w
        matches = [w for w in wins
                   if ref_l in (w.get("title") or "").lower()
                   or ref_l == (w.get("app") or "").lower()]
        return matches[0] if len(matches) == 1 else (matches or [None])[0]
