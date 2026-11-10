"""Unit tests for riko/touch_desk.py: the press-arbitration state machine on
synthetic event sequences + the sit-edge maths (docs/TOUCH.md). Pure Python:
no Qt, no compositor, no clock — exactly the contract's 'keep the Qt glue
thin, unit-test the state machine'."""
import json
import os

import pytest

from riko import touch_desk as td

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

BBOX = {"x": 100, "y": 50, "w": 200, "h": 500, "hips_y": 470,
        "parts": {"head": {"x": 170, "y": 60, "w": 60, "h": 60},
                  "hand_left": [60, 250, 40, 40],
                  "torso": {"x": 150, "y": 130, "w": 100, "h": 150}}}   # not a limb


# ── limb lookup ────────────────────────────────────────────────────────────

def test_limb_at_head_and_hand():
    assert td.limb_at(BBOX, 200, 90) == "head"
    assert td.limb_at(BBOX, 70, 270) == "hand_left"
    assert td.limb_at(BBOX, 120, 120) is None       # window, not a limb
    assert td.limb_at(BBOX, 200, 200) is None        # torso: not in LIMB_PARTS


def test_limb_at_junk_bbox():
    for junk in (None, {}, {"parts": None}, {"parts": []},
                 {"parts": {"head": "nope"}}, {"parts": {"head": [1, 2]}},
                 {"parts": {"head": {"x": 0, "y": 0, "w": 0, "h": 10}}}):
        assert td.limb_at(junk, 5, 5) is None


# ── arbitration: press elsewhere = instant move (the old feel) ────────────

def test_press_outside_limb_moves_immediately():
    a = td.PressArbiter()
    assert a.press(120, 120, 1000, inside_limb=False) == td.MOVE
    assert a.state == a.MOVING
    assert a.motion(180, 130, 1010) == "none"       # moves while moving: inert
    assert a.release(1200) == "none"
    assert a.state == a.IDLE


# ── arbitration: press on a limb ──────────────────────────────────────────

def test_hold_still_then_page_grabs():
    a = td.PressArbiter()                          # hold_ms=250, move_px=6
    assert a.press(200, 90, 1000, inside_limb=True) == td.DEFER
    assert a.pending
    assert a.motion(202, 91, 1100) == "none"        # tiny move, still deciding
    assert a.motion(201, 90, 1260) == "grab"        # >= 250 ms: the page's now
    assert a.state == a.GRABBED
    assert a.motion(260, 90, 1300) == "none"        # a later big move is a grab, not a drag
    assert a.release(1500) == "none"


def test_quick_move_within_hold_window_becomes_drag():
    a = td.PressArbiter()
    assert a.press(200, 90, 1000, inside_limb=True) == td.DEFER
    assert a.motion(207, 90, 1240) == td.MOVE       # 7 px at 240 ms (< 250)
    assert a.state == a.MOVING


def test_move_after_hold_window_is_a_grab_not_a_drag():
    a = td.PressArbiter()
    a.press(200, 90, 1000, inside_limb=True)
    assert a.motion(300, 300, 1251) == "grab"       # dt >= 250 beats distance
    assert a.state == a.GRABBED


def test_exact_thresholds():
    a = td.PressArbiter()
    a.press(100, 100, 0, inside_limb=True)
    assert a.motion(106, 100, 249.9) == "none"      # exactly 6 px is NOT > 6
    a.press(100, 100, 0, inside_limb=True)
    assert a.motion(107, 100, 249.9) == td.MOVE     # 7 px within window: drag
    a.press(100, 100, 0, inside_limb=True)
    assert a.motion(107, 100, 250) == "grab"        # exactly 250 ms: hold wins


def test_release_before_hold_is_a_tap_and_resets():
    a = td.PressArbiter()
    a.press(200, 90, 1000, inside_limb=True)
    assert a.motion(202, 91, 1050) == "none"
    assert a.release(1120) == "tap"                 # page saw the whole click
    assert a.state == a.IDLE
    assert a.press(120, 120, 1200, inside_limb=False) == td.MOVE  # clean slate


def test_tick_resolves_still_press_without_motion():
    a = td.PressArbiter()
    a.press(200, 90, 1000, inside_limb=True)
    assert a.tick(1100) == "none"
    assert a.tick(1251) == "grab"
    assert a.tick(1300) == "none"                   # already decided


def test_second_press_while_pending_passes_to_page():
    a = td.PressArbiter()
    a.press(200, 90, 1000, inside_limb=True)
    assert a.press(120, 120, 1050, inside_limb=False) == td.PASS
    assert a.pending                                # still deciding the first press


def test_release_ends_moving_state():
    a = td.PressArbiter()
    a.press(200, 90, 1000, inside_limb=True)
    a.motion(240, 160, 1100)                        # -> MOVING
    assert a.release(1400) == "none"
    assert a.state == a.IDLE


# ── sit-edge maths ─────────────────────────────────────────────────────────

def test_edge_y_css_and_dpr():
    # monitor bottom 1080, window top 500: 580 CSS px of window below the edge
    assert td.edge_y_css(1080, 500) == 580
    # physical px from hyprctl with scale 1.25 -> logical/CSS px
    assert td.edge_y_css(1080, 500, dpr=1.25) == pytest.approx(580 / 1.25)
    # a window entirely below the edge: negative -> any hips >= edge: sit
    assert td.edge_y_css(1080, 1100) == -20


def test_sit_watcher_sits_at_edge_and_stands_when_lifted():
    w = td.SitWatcher()
    assert w.update(300, 580) is None               # hips well above the edge
    assert w.update(574, 580) == "sit:580"          # 574 >= 580 - 8
    assert w.sitting
    assert w.update(575, 580) is None               # unchanged edge: no spam
    assert w.update(580, 583) is None               # edge moved 3 px: no re-emit
    assert w.update(580, 587) == "sit:587"          # >= 4 px: re-emit
    assert w.update(548, 587) is None               # 548 >= 587-40: still sitting
    assert w.update(546, 587) == "stand"            # < edge - 40: stood up
    assert not w.sitting
    assert w.update(535, 580) is None               # standing, hips not at edge


def test_sit_watcher_ignores_missing_hips():
    w = td.SitWatcher()
    assert w.update(None, 580) is None
    assert w.update(600, None) is None
    assert not w.sitting
    assert w.update(600, 580) == "sit:580"


def test_sit_watcher_stands_offscreen_window_bottom():
    # window dragged so its top is below the monitor bottom: edge <= 0, so
    # any posted hips (>= 0) sit; lifting the hips needs hips < edge - 40.
    w = td.SitWatcher()
    assert w.update(0, -20) == "sit:-20"
    assert w.sitting


# ── hyprctl monitor lookup (recorded fixtures only) ───────────────────────

@pytest.fixture
def hypr_monitors():
    with open(os.path.join(FIX, "hypr_real_monitors.json")) as fh:
        return json.load(fh)


@pytest.fixture
def hypr_clients():
    with open(os.path.join(FIX, "hypr_real_clients_riko.json")) as fh:
        return json.load(fh)


def test_monitor_from_hypr_fixture(hypr_monitors, hypr_clients):
    own = next(c for c in hypr_clients if c.get("class") == "riko")
    at = own["at"]
    mon = td.monitor_from_hypr(hypr_monitors, at)
    assert mon is not None
    assert mon["bottom"] > at[1]
    assert mon["scale"] >= 1.0
    # edge maths on the owner's recorded numbers: window hangs below the
    # screen bottom -> small/negative edge in CSS px after dividing by scale
    edge = td.edge_y_css(mon["bottom"], at[1], dpr=mon["scale"])
    assert edge == (mon["bottom"] - at[1]) / mon["scale"]


def test_monitor_from_hypr_picks_containing_then_overlap(hypr_monitors):
    ms = [dict(m) for m in hypr_monitors if not m.get("disabled")]
    first = ms[0]
    x, y = first["x"], first["y"]
    assert td.monitor_from_hypr(ms, [x + 5, y + 5])["name"] == first["name"]
    assert td.monitor_from_hypr(ms, [-10_000, -10_000]) is None   # nowhere near
    assert td.monitor_from_hypr([], [0, 0]) is None
    assert td.monitor_from_hypr(ms, "junk") is None
