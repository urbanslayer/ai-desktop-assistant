"""End-to-end touch seam (docs/TOUCH.md): a REAL /control/bbox parts payload
posted by the page (captured headless from the live rig, saved as
fixtures/touch_bbox_parts.json) drives the desktop-side consumers — the press
arbiter's limb_at() must resolve the hand/head rects, refuse the torso, and
the SitWatcher must engage for the payload's hips_y against a nearby edge.

This is the seam the reviewer blocked in round T1: in NON-roam desktop mode
the page used to post nothing at all, so both consumers silently never fired.
"""
import os
import pathlib

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
             "SWAYSOCK", "DBUS_SESSION_BUS_ADDRESS"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402

from riko import touch_desk  # noqa: E402

FX = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def bbox():
    import json
    return json.loads((FX / "touch_bbox_parts.json").read_text())


def _center(rect):
    x, y, w, h = rect
    return (x + w / 2, y + h / 2)


def test_payload_contract(bbox):
    assert {"x", "y", "w", "h", "parts", "hips_y", "feet_y"} <= bbox.keys()
    parts = bbox["parts"]
    for limb in ("head", "hand_l", "hand_r"):
        assert limb in parts and len(parts[limb]) == 4


def test_limb_at_resolves_logged_rect_centers(bbox):
    parts = bbox["parts"]
    for part in ("hand_l", "hand_r", "head"):
        hit = touch_desk.limb_at(bbox, *_center(parts[part]))
        assert hit == part, f"{part} rect centre resolved to {hit!r}"


def test_limb_at_refuses_hips_midline(bbox):
    # a press dead-centre on the hips column must not resolve to any limb:
    # the arbiter only DEFERs on limbs, everything else drags the window
    hx, hy, hw, hh = bbox["parts"]["hips"]
    midline = (hx + hw / 2, hy + hh / 2)
    hit = touch_desk.limb_at(bbox, *midline)
    assert hit not in ("hand_l", "hand_r", "arm_l", "arm_r", "head",
                       "foot_l", "foot_r")


def test_press_arbiter_grabs_logged_hands_and_drags_torso(bbox):
    parts = bbox["parts"]
    arb = touch_desk.PressArbiter()
    for part in ("hand_l", "hand_r"):
        cx, cy = _center(parts[part])
        assert touch_desk.limb_at(bbox, cx, cy) == part
        assert arb.press(cx, cy, 0.0, inside_limb=True) == touch_desk.DEFER
        assert arb.tick(100.0) == "none"           # under HOLD_MS: still deciding
        assert arb.tick(touch_desk.HOLD_MS) == "grab"
        assert arb.state == "grabbed"              # page owns the limb
        assert arb.release(touch_desk.HOLD_MS + 50) == "none"
    # hips press (limb_at None): instant window-move, never a limb grab
    hx, hy, hw, hh = parts["hips"]
    arb2 = touch_desk.PressArbiter()
    assert touch_desk.limb_at(bbox, hx + hw / 2, hy + hh / 2) not in (
        "hand_l", "hand_r", "head")
    assert arb2.press(hx + hw / 2, hy + hh / 2, 0.0,
                      inside_limb=False) == touch_desk.MOVE
    assert arb2.tick(touch_desk.HOLD_MS + 10) == "none"   # moving: no grab


def test_sit_watcher_sits_for_the_logged_hips_y(bbox):
    # edge line just ABOVE the logged hips (smaller y): watcher must sit,
    # then stand only once hips climb back above stand_gap
    w = touch_desk.SitWatcher()
    edge = bbox["hips_y"] - 8
    cmd = w.update(bbox["hips_y"], edge)
    assert cmd == f"sit:{round(edge)}"
    assert w.sitting and w.edge_y == pytest.approx(edge)
    # tracking the dragged window: edge moves -> republish
    cmd2 = w.update(bbox["hips_y"], edge - 20)
    assert cmd2 == f"sit:{round(edge - 20)}"
    cmd3 = w.update(edge - 20 - touch_desk.STAND_GAP - 2, edge - 20)
    assert cmd3 == "stand"
    assert not w.sitting


def test_sit_watcher_ignores_missing_seam(bbox):
    # the exact non-roam failure mode: no hips_y posted -> no verdict ever
    w = touch_desk.SitWatcher()
    assert w.update(None, 500) is None
    assert not w.sitting
