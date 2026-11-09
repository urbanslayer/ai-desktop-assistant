"""Window-id formats across the hyprctl paths (typing bug 2, 2026-11-09).

The typing engine keys its per-window memory (_last_line, run tokens) on the
window id. The same kitty window arrives as '0x5642c7991580' from one path
and '0x5642C7991580' from another (and a model happily echoes the address
back with the 0x stripped), which silently orphaned the typed line at the
Enter ask: 'unknown command — Riko did not type this line'. These fixtures
lock the backend to one canonical shape on every path. Real hyprctl JSON
shapes, a fake runner; hyprctl and ydotool are never executed."""
import json
from types import SimpleNamespace

import pytest

from riko.desktop_sense import hyprland as hyprland_mod
from riko.desktop_sense.util import norm_window_id

LOWER = "0x5642c7991580"
UPPER = "0x5642C7991580"
BARE = "5642c7991580"


def _done(out, rc=0):
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


class Done(Exception):
    def __init__(self, out):
        self.out = out


def run_hypr(responses):
    calls = []

    def runner(argv, capture=True, env=None, timeout=None):
        key = tuple(argv)
        calls.append(list(argv))
        if key in responses:
            r = responses[key]
            if isinstance(r, Done):
                return _done(r.out)
            return _done(r)
        raise AssertionError(f"unexpected hyprcall: {key}")

    runner.calls = calls
    return runner


def client_entry(address):
    """one real `hyprctl clients -j` entry"""
    return {"address": address, "id": 1, "title": "user@arch: ~",
            "class": "kitty", "initialClass": "org.kde.kitty",
            "initialTitle": "kitty", "pid": 4412, "workspace": {"id": 1,
            "name": "1"}, "monitor": 0, "focusHistoryID": 0,
            "fullscreen": False, "floating": False}


def active_entry(address):
    """one real `hyprctl activewindow -j` reply"""
    return {"address": address, "class": "kitty", "title": "user@arch: ~",
            "workspace": {"id": 1, "name": "1"}, "monitor": 0, "pid": 4412,
            "fullscreen": False, "floating": {"state": 0}}


MONITORS = [{"id": 0, "name": "eDP-1", "description": "BOE 0x097B",
             "size": {"x": 2880, "y": 1800}, "position": {"x": 0, "y": 0},
             "scale": 1.0, "transform": 0, "focused": True,
             "activeWorkspace": {"id": 1, "name": "1"}, "disabled": False}]


def base_replies(clients_addr, active_addr):
    return {
        ("hyprctl", "clients", "-j"):
            json.dumps([client_entry(clients_addr)]),
        ("hyprctl", "activewindow", "-j"): json.dumps(active_entry(active_addr)),
        ("hyprctl", "activeworkspace", "-j"):
            json.dumps({"id": 1, "name": "1", "monitor": 0}),
        ("hyprctl", "workspaces", "-j"):
            json.dumps([{"id": 1, "name": "1", "monitor": 0}]),
        ("hyprctl", "monitors", "-j"): json.dumps(MONITORS),
    }


def backend(clients_addr, active_addr, extra=None, style="legacy"):
    replies = base_replies(clients_addr, active_addr)
    replies.update(extra or {})
    runner = run_hypr(replies)
    b = hyprland_mod.HyprlandBackend(runner=runner, lua=None,
                                     config_dir="/nonexistent-riko-test",
                                     config_style=style)
    return b, runner


# ── read paths emit ONE canonical shape ────────────────────────
@pytest.mark.parametrize("clients_addr,active_addr", [
    (UPPER, LOWER),      # activewindow lowercase, clients uppercase
    (LOWER, UPPER),      # and the other way round
    (UPPER, UPPER),
])
def test_windows_and_focused_share_one_id(clients_addr, active_addr):
    b, _ = backend(clients_addr, active_addr)
    focused = b.focused()
    listing = b.windows()
    assert len(listing) == 1
    assert listing[0]["id"] == LOWER == norm_window_id(clients_addr)
    assert focused["id"] == LOWER
    assert listing[0]["focused"] is True       # formats still MATCH
    assert focused["id"] == listing[0]["id"]   # what the engine compares


def test_focus_window_accepts_the_bare_address_and_dispatches_canonical():
    """A model echoes the id without 0x; the dispatch must still carry the
    exact lowercase address Hyprland expects."""
    b, runner = backend(LOWER, LOWER, extra={
        ("hyprctl", "dispatch", f"focuswindow address:{LOWER}"): Done("ok")})
    out = b.focus_window(BARE)
    assert out["address"] == LOWER
    assert ["hyprctl", "dispatch", f"focuswindow address:{LOWER}"] in runner.calls


def test_focus_window_accepts_uppercase_ref():
    b, runner = backend(UPPER, UPPER, extra={
        ("hyprctl", "dispatch", f"focuswindow address:{LOWER}"): Done("ok")})
    out = b.focus_window(UPPER)
    assert out["address"] == LOWER


def test_lua_focus_target_is_canonical():
    """The Lua path feeds the address into a quoted string — one stray
    uppercase digit means 'no window matches' inside Hyprland."""
    seen = {}

    class LuaOn:
        preferred = {}

    b, _ = backend(UPPER, UPPER, style="lua")
    b._lua, b._lua_tried = LuaOn(), True

    def rec_dispatch(expr, runner=None, what="dispatch"):
        seen["expr"] = expr
        return "ok"

    b._dispatch_raw = lambda expr, runner=None: (seen.__setitem__("expr", expr),
                                                 "ok")[1]
    out = b.focus_window(BARE)
    assert seen["expr"] == f'hl.dsp.focus({{ window = "address:{LOWER}" }})'
    assert out["address"] == LOWER


def test_norm_window_id_is_the_single_normaliser():
    assert norm_window_id(UPPER) == LOWER
    assert norm_window_id(BARE) == LOWER
    assert norm_window_id(f" address:{UPPER} ") == LOWER
    assert norm_window_id(LOWER) == LOWER
    assert norm_window_id("org.kde.kitty") == "org.kde.kitty"   # classes pass
    assert norm_window_id(None) == ""
