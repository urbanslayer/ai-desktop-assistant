"""Scroll-to-size the window (not the viewport): the pure math, the Hyprland
dispatches (fake hyprctl runner — never the live session), the roam strip
height, and the per-persona persistence.

The page-side wheel math lives in client/js/scale.test.js (node --test).
"""
import json
from types import SimpleNamespace

import pytest

from riko import appstate, avatar_scale, roam


# ── clamp / fit ──────────────────────────────────────────────────────────
def test_clamp_scale_band_and_junk():
    assert avatar_scale.clamp_scale(1.0) == 1.0
    assert avatar_scale.clamp_scale(0.1) == avatar_scale.SCALE_MIN
    assert avatar_scale.clamp_scale(99) == avatar_scale.SCALE_MAX
    for junk in (None, "big", {}, float("nan"), float("inf")):
        assert avatar_scale.clamp_scale(junk) == avatar_scale.SCALE_DEFAULT


def test_effective_scale_clamps_to_monitor_fit():
    mon = (0, 0, 1920, 1080)
    m = avatar_scale.MONITOR_MARGIN
    # 2.5x would be 1050x1700 — taller than the monitor: capped by HEIGHT
    fit = avatar_scale.effective_scale(mon, 420, 680, 2.5)
    assert fit == pytest.approx((1080 - 2 * m) / 680)
    # a tiny monitor caps WIDTH first and the band edges still apply below
    fit_w = avatar_scale.effective_scale((0, 0, 500, 2000), 420, 680, 2.0)
    assert fit_w == pytest.approx((500 - 2 * m) / 420)
    # inside the band and inside the monitor: the request passes through
    assert avatar_scale.effective_scale(mon, 420, 680, 1.2) == 1.2


# ── feet-anchored geometry ───────────────────────────────────────────────
MON = (0, 0, 1920, 1080)
CUR = (700, 380, 420, 680)          # bottom-centre (feet) at (910, 1060)


def test_scaled_geometry_anchors_the_feet():
    for scale in (0.5, 1.0, 1.5):
        x, y, w, h = avatar_scale.scaled_geometry(MON, CUR, 420, 680, scale)
        assert w == round(420 * scale) and h == round(680 * scale)
        assert x + w / 2 == pytest.approx(910, abs=0.5)   # same centre line
        assert y + h == 1060                              # feet stay put
        # the whole window stays on the monitor
        assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080


def test_scaled_geometry_stops_growing_at_the_monitor():
    # 2.0x would be 840x1360 on a 1080-high monitor: clamped to the fit cap,
    # still aspect-exact, fully on screen, feet as low as they can go
    x, y, w, h = avatar_scale.scaled_geometry(MON, CUR, 420, 680, 2.0)
    assert h == 1080 - 2 * avatar_scale.MONITOR_MARGIN
    assert w == round(420 * h / 680)
    assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080


def test_scaled_geometry_clamped_offscreen_window_stays_visible():
    # window near the bottom edge: growing upward runs out of room, the
    # result is clamped fully inside the monitor instead of hanging off it
    cur = (1500, 1000, 420, 680)     # bottom 1680 is already below (odd pos)
    x, y, w, h = avatar_scale.scaled_geometry(MON, cur, 420, 680, 1.0)
    assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080


# ── Hyprland dispatches (own window only; fake runner) ───────────────────
def test_dispatches_lua_select_window_by_address():
    exprs = avatar_scale.hypr_scale_dispatches("lua", 630, 1020, 595, 40,
                                               address="0xabc123")
    assert exprs == [
        'hl.dsp.window.resize({x=630, y=1020, window="address:0xabc123"})',
        'hl.dsp.window.move({x=595, y=40, window="address:0xabc123"})',
    ]
    assert all("address:0xabc123" in e for e in exprs)   # never another window


def test_dispatches_legacy_select_window_by_class():
    exprs = avatar_scale.hypr_scale_dispatches("legacy", 630, 1020, 595, 40,
                                               address="", app_id="riko")
    assert exprs == [
        '"resizewindowpixel exact 630 1020,class:^(riko)$"',
        '"movewindowpixel exact 595 40,class:^(riko)$"',
    ]


def _fake_hypr(rico_at=(700, 380), rico_size=(420, 680), monitor_id=0,
               dispatch_reply="ok", extra_windows=True):
    """Fake hyprctl runner: one riko window plus decoys; records argv."""
    clients = [{"class": "firefox", "address": "0xff00", "at": [0, 0],
                "size": [800, 600], "monitor": monitor_id}]
    if extra_windows:
        clients.append({"class": "kitty", "address": "0xbee", "at": [0, 0],
                        "size": [800, 600], "monitor": monitor_id})
    clients.append({"class": "riko", "address": "0x1234abcd",
                    "at": list(rico_at), "size": list(rico_size),
                    "monitor": monitor_id, "floating": True})
    monitors = [{"id": 0, "name": "eDP-1", "x": 0, "y": 0,
                 "width": 1920, "height": 1080, "scale": 1, "transform": 0,
                 "reserved": [0, 0, 0, 0], "disabled": False},
                {"id": 1, "name": "DP-3", "x": 1920, "y": 0,
                 "width": 2560, "height": 1440, "scale": 1, "transform": 0,
                 "reserved": [0, 0, 0, 0], "disabled": False}]
    seen = []

    def runner(argv, timeout=10, **kw):
        d = SimpleNamespace(returncode=0, stderr="", stdout="")
        seen.append(list(argv))
        if argv[:3] == ["hyprctl", "clients", "-j"]:
            d.stdout = json.dumps(clients)
        elif argv[:3] == ["hyprctl", "monitors", "-j"]:
            d.stdout = json.dumps(monitors)
        elif argv[:2] == ["hyprctl", "dispatch"]:
            d.stdout = dispatch_reply
        return d
    return runner, seen


def _fake_style_dir(tmp_path, style):
    d = tmp_path / f"hypr-{style}"
    d.mkdir()
    (d / ("hyprland.lua" if style == "lua" else "hyprland.conf")).write_text(
        "// fixture\n", encoding="utf-8")
    return d


@pytest.mark.parametrize("style", ["lua", "legacy"])
def test_apply_hyprland_scale_feet_anchored(tmp_path, style):
    runner, seen = _fake_hypr()
    res = avatar_scale.apply_hyprland_scale(
        1.5, 420, 680, runner=runner, config_dir=str(_fake_style_dir(tmp_path, style)))
    assert res["ok"] is True, res
    assert res["geometry"] == (595, 40, 630, 1020)      # feet at (910,1060) kept
    assert len(res["dispatched"]) == 2                  # resize THEN move
    # every dispatch that went to the compositor is one of ours (the legacy
    # style probe 'jitter' may also appear; nothing else does)
    fired = [a[2] for a in seen if a[:2] == ["hyprctl", "dispatch"]
             and a[2] != "jitter"]
    assert fired == res["dispatched"]
    if style == "lua":
        assert 'address:0x1234abcd' in res["dispatched"][0]
    else:
        assert "class:^(riko)$" in res["dispatched"][0]


def test_apply_hyprland_scale_uses_the_windows_own_monitor():
    # the riko window lives on monitor 1 (1920,0,2560,1440): the fit clamp
    # and the on-screen clamp use THAT monitor, not the first row
    runner, _ = _fake_hypr(monitor_id=1)
    res = avatar_scale.apply_hyprland_scale(1.5, 420, 680, runner=runner,
                                            style="lua")
    assert res["ok"] is True
    x, y, w, h = res["geometry"]
    assert 1920 <= x and x + w <= 1920 + 2560 and 0 <= y and y + h <= 1440


def test_apply_hyprland_scale_requires_ok_reply_and_mapped_window():
    runner, _ = _fake_hypr(dispatch_reply="no active window")
    res = avatar_scale.apply_hyprland_scale(1.2, 420, 680, runner=runner,
                                            style="lua")
    assert res["ok"] is False and "no active window" in res["error"]

    runner2, _ = _fake_hypr(extra_windows=False)

    def nomap(argv, timeout=10, **kw):
        return SimpleNamespace(returncode=0, stderr="", stdout="[]")
    res2 = avatar_scale.apply_hyprland_scale(1.2, 420, 680, runner=nomap,
                                             style="lua")
    assert res2["ok"] is False and "not mapped" in res2["error"]


# ── REAL hyprctl rows (fixtures captured from live Hyprland 0.56.2) ──────
# The old invented monitor shape (`size: {x,y}` / `recommendedMode`) does
# not exist in real `hyprctl -j monitors` output: rows carry PHYSICAL
# width/height + scale + transform + reserved bars; window coords and the
# resize/move dispatches are all LOGICAL layout px (proof: the real riko
# row is size [420,680] == the requested base size on the 1.25-scale
# monitor). fixtures/hypr_real_monitors.json + hypr_real_clients_riko.json:
# DP-2 id0 x0 2560x1440 s1 | DP-3 id2 x2560 2560x1440 s1 |
# HDMI-A-1 id1 x5120 2560x1440 s1.25 -> logical 2048x1152; all reserved
# [0,44,0,0] (top bar). The riko row: monitor 1, at [6675,227], size
# [420,680].
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _real_rows():
    monitors = json.loads((FIXTURES / "hypr_real_monitors.json")
                          .read_text(encoding="utf-8"))
    clients = json.loads((FIXTURES / "hypr_real_clients_riko.json")
                         .read_text(encoding="utf-8"))
    return monitors, clients[0]


def _real_runner(dispatch_reply="ok"):
    monitors, own = _real_rows()

    def runner(argv, timeout=10, **kw):
        d = SimpleNamespace(returncode=0, stderr="", stdout="")
        if argv[:3] == ["hyprctl", "clients", "-j"]:
            d.stdout = json.dumps([own])
        elif argv[:3] == ["hyprctl", "monitors", "-j"]:
            d.stdout = json.dumps(monitors)
        elif argv[:2] == ["hyprctl", "dispatch"]:
            d.stdout = dispatch_reply
        return d
    return runner


# usable logical rect of the 1.25-scale HDMI-A-1: x 5120, y 0+44 (bar),
# 2560/1.25=2048 wide, 1440/1.25-44=1108 tall
HDMI = (5120, 44, 2048, 1108)


def test_monitor_logical_rect_real_rows():
    monitors, own = _real_rows()
    rects = {m["id"]: avatar_scale.monitor_logical_rect(m) for m in monitors}
    assert rects[0] == (0, 44, 2560, 1396)        # DP-2: scale 1, minus 44px bar
    assert rects[2] == (2560, 44, 2560, 1396)     # DP-3
    assert rects[1] == HDMI                        # HDMI-A-1: scale 1.25 applied
    disabled = dict(monitors[0], disabled=True)
    assert avatar_scale.monitor_logical_rect(disabled) is None
    # rotated transforms swap w/h (1/3/5/7 = 90/270)
    assert avatar_scale.monitor_logical_rect(
        {"width": 2560, "height": 1440, "scale": 1, "transform": 1,
         "x": 0, "y": 0}) == (0, 0, 1440, 2560)
    assert avatar_scale.monitor_logical_rect(
        {"width": 2560, "height": 1440, "scale": 1, "transform": 3,
         "x": 0, "y": 0}) == (0, 0, 1440, 2560)
    # scale applies AFTER the swap
    assert avatar_scale.monitor_logical_rect(
        {"width": 2560, "height": 1440, "scale": 1.25, "transform": 5,
         "x": 0, "y": 0}) == (0, 0, 1152, 2048)
    # transform 0/2/4/6 do NOT swap
    assert avatar_scale.monitor_logical_rect(
        {"width": 2560, "height": 1440, "scale": 1, "transform": 2,
         "x": 0, "y": 0}) == (0, 0, 2560, 1440)


def test_monitor_logical_rect_reserved_bars_shrink_the_rect():
    # reserved is [left, top, right, bottom] -> the usable rect is inset
    m = {"width": 1920, "height": 1080, "scale": 1, "transform": 0,
         "x": 100, "y": 200, "reserved": [10, 20, 30, 40]}
    assert avatar_scale.monitor_logical_rect(m) == (110, 220, 1880, 1020)
    # a row whose bars eat the whole area is unusable, not a negative rect
    assert avatar_scale.monitor_logical_rect(
        dict(m, reserved=[1000, 500, 1000, 500])) is None


def test_monitor_rect_for_real_client_row():
    monitors, own = _real_rows()
    assert avatar_scale.monitor_rect_for(own, monitors) == HDMI


def test_monitor_rect_for_falls_back_to_window_centre():
    monitors, own = _real_rows()
    # monitor -1 / missing (window not assigned): the monitor whose usable
    # logical rect contains the window centre wins — here HDMI-A-1
    # (centre 6885, 567 inside 5120..7168 x 44..1152)
    assert avatar_scale.monitor_rect_for(dict(own, monitor=-1), monitors) == HDMI
    no_key = {k: v for k, v in own.items() if k != "monitor"}
    assert avatar_scale.monitor_rect_for(no_key, monitors) == HDMI
    # a bogus id falls back the same way (id lookup missed, centre matches)
    assert avatar_scale.monitor_rect_for(dict(own, monitor=99), monitors) == HDMI
    # centre on no monitor and no overlap -> None (not a random first row)
    assert avatar_scale.monitor_rect_for(
        dict(own, monitor=-1, at=[50000, 50000]), monitors) is None


def test_monitor_rect_for_skips_disabled_monitor():
    monitors, own = _real_rows()
    mons = [dict(m, disabled=(m["id"] == 1)) for m in monitors]
    # id 1 disabled: neither the id match nor the centre fallback may use it
    # (the window rect lies fully right of DP-3, so nothing overlaps -> None)
    assert avatar_scale.monitor_rect_for(own, mons) is None


def test_apply_hyprland_scale_real_rows_clamps_on_scaled_monitor():
    # 2.5x on HDMI-A-1: usable height 1152 minus the 44px bar minus margins
    # -> max height 1068 logical px (NOT 1440 physical and not 1152-full)
    runner = _real_runner()
    res = avatar_scale.apply_hyprland_scale(2.5, 420, 680, runner=runner,
                                            style="lua")
    assert res["ok"] is True, res
    x, y, w, h = res["geometry"]
    m = avatar_scale.MONITOR_MARGIN
    assert h == 1152 - 44 - 2 * m == 1068
    assert w == round(420 * h / 680) == 660        # aspect kept
    rx, ry, rw, rh = HDMI
    assert rx <= x and x + w <= rx + rw and ry <= y and y + h <= ry + rh
    # dispatches carry the LOGICAL px (e.g. 660x1068), never physical
    assert f"x={w}, y={h}" in res["dispatched"][0]


def test_apply_hyprland_scale_real_rows_feet_anchored():
    # 1.08x fits: feet stay at (6885, 907) — the window's current bottom
    runner = _real_runner()
    res = avatar_scale.apply_hyprland_scale(1.08, 420, 680, runner=runner,
                                            style="lua")
    assert res["ok"] is True, res
    x, y, w, h = res["geometry"]
    assert (w, h) == (454, 734)
    assert y + h == 227 + 680                       # feet anchored
    assert x + w / 2 == pytest.approx(6675 + 420 / 2, abs=0.5)
    rx, ry, rw, rh = HDMI
    assert rx <= x and x + w <= rx + rw and ry <= y and y + h <= ry + rh


def test_apply_hyprland_scale_invented_keys_now_diagnosable():
    # regression for the live '[size] hyprland: monitor for own window
    # unknown' failure: a row in the OLD invented shape (no width/height)
    # must produce an error line carrying the numbers, not a bare message
    _, own = _real_rows()
    fake_mon = [{"id": 1, "name": "HDMI-A-1", "x": 5120, "y": 0,
                 "size": {"x": 2560, "y": 1440}}]

    def runner(argv, timeout=10, **kw):
        d = SimpleNamespace(returncode=0, stderr="", stdout="")
        if argv[:3] == ["hyprctl", "clients", "-j"]:
            d.stdout = json.dumps([own])
        elif argv[:3] == ["hyprctl", "monitors", "-j"]:
            d.stdout = json.dumps(fake_mon)
        return d
    res = avatar_scale.apply_hyprland_scale(1.2, 420, 680, runner=runner,
                                            style="lua")
    assert res["ok"] is False
    err = res["error"]
    assert "monitor for own window unknown" in err
    assert "window monitor id 1" in err and "monitor ids [1]" in err
    assert "'size'" in err and "keys present" in err


def test_apply_hyprland_scale_unmapped_error_lists_classes():
    def runner(argv, timeout=10, **kw):
        d = SimpleNamespace(returncode=0, stderr="", stdout="")
        if argv[:3] == ["hyprctl", "clients", "-j"]:
            d.stdout = json.dumps([{"class": "firefox"}, {"class": "kitty"}])
        return d
    res = avatar_scale.apply_hyprland_scale(1.2, 420, 680, runner=runner,
                                            style="lua")
    assert res["ok"] is False and "not mapped" in res["error"]
    assert "['firefox', 'kitty']" in res["error"]


# ── roam strip height follows the scale ──────────────────────────────────
def test_strip_geometry_follows_scale_bottom_anchored():
    geo = (0, 0, 1920, 1080)
    assert roam.strip_geometry(geo, 680, 1.0) == (0, 432, 1920, 648)   # 60% cap
    assert roam.strip_geometry(geo, 680, 0.4) == (0, 808, 1920, 272)   # 272 = 680*0.4
    assert roam.strip_geometry(geo, 680, 2.5) == (0, 432, 1920, 648)   # capped
    x, y, w, h = roam.strip_geometry(geo, 680, 1.5)
    assert (x, w) == (0, 1920) and y + h == 1080                       # full width, bottom edge
    tiny = roam.strip_geometry(geo, 300, 0.4)                          # 120 -> min 200
    assert tiny[3] == 200 and tiny[1] + tiny[3] == 1080
    strip_small_monitor = roam.strip_geometry((0, 0, 800, 300), 680, 2.0)
    assert strip_small_monitor[3] <= 300                               # never taller than the screen


# ── persistence per persona ──────────────────────────────────────────────
def test_scale_persists_per_persona(monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "state"))
    assert appstate.load_scale("riko") is None          # never chosen
    assert appstate.save_scale("riko", 1.5) is True
    assert appstate.save_scale("asuka", 0.8) is True
    assert appstate.load_scale("riko") == 1.5
    assert appstate.load_scale("asuka") == 0.8          # independent
    assert appstate.load_scale("other") is None
    assert appstate.save_scale("riko", 1.2) is True     # last write wins
    assert appstate.load_scale("riko") == 1.2 and appstate.load_scale("asuka") == 0.8


def test_scale_persistence_rejects_junk(monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "state"))
    for bad in (0.2, 3.0, "big", None, True, float("nan")):
        assert appstate.save_scale("riko", bad) is False, bad
    # a hand-mangled file reads as "never chosen", never raises
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "state" / "avatar_scale.json"
    for junk in ("{ not json", "[1,2]", '{"riko": "big"}', '{"riko": 9}'):
        p.write_text(junk, encoding="utf-8")
        assert appstate.load_scale("riko") is None
