"""Tests for riko/avatar_scale.py — the desktop-side scroll-zoom window math.

The page renders the zoom; this module sizes the WINDOW at gesture
boundaries. Covered: the scale bands, the monitor-fit clamp (real 0.56.2
rows), feet-anchored geometry, the ONE-batched-hyprctl-per-apply discipline,
monitor/window caching (gesture grow→fit lands feet-anchored with exactly
one dispatch per boundary), and failure → cache invalidation. No compositor,
no window, no screenshot: fake hyprctl runners only.
"""
import json
import math
import os
import time

from riko import avatar_scale as asc

BASE_W, BASE_H = 420, 680

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(FIX, "hypr_real_monitors.json")) as fh:
    REAL_MONITORS = json.load(fh)
with open(os.path.join(FIX, "hypr_real_clients_riko.json")) as fh:
    REAL_CLIENTS = json.load(fh)

# monitor 1 = HDMI-A-1: 2560x1440 physical @ 1.25 scale at x=5120, a 44 px
# top bar reserved → usable LOGICAL rect (the exact monitor the owner runs
# Riko on; her window row [6675, 227, 420, 680] is already logical-space)
MON1 = asc.monitor_logical_rect(REAL_MONITORS[1])
assert MON1 is not None                      # fixture sanity for every test below

OWN = next(c for c in REAL_CLIENTS if c.get("class") == "riko")
assert OWN is not None


# ── the bands ─────────────────────────────────────────────────────────

def test_clamp_scale_band_and_junk():
    assert asc.clamp_scale(0.1) == asc.SCALE_MIN
    assert asc.clamp_scale(3.0) == asc.SCALE_MAX
    assert asc.clamp_scale(1.25) == 1.25
    for junk in (None, "", "abc", float("nan"), float("inf"), -float("inf"), True, object()):
        assert asc.clamp_scale(junk) == asc.SCALE_DEFAULT


def test_clamp_window_scale_allows_gesture_headroom():
    assert asc.clamp_window_scale(0.1) == asc.SCALE_MIN
    # headroom above SCALE_MAX is legal for WINDOW scales (the monitor
    # clamp bounds it for real); junk still defaults to 1.0
    assert asc.clamp_window_scale(2.5 * 1.6) == 4.0   # 4.0 < the 5.0 ceiling
    assert asc.clamp_window_scale(99) == asc.WINDOW_SCALE_MAX
    assert asc.clamp_window_scale(None) == asc.SCALE_DEFAULT
    assert asc.clamp_window_scale(float("nan")) == asc.SCALE_DEFAULT


# ── monitor fit clamp ─────────────────────────────────────────────────

def test_monitor_fit_clamp():
    s = asc.effective_scale(MON1, BASE_W, BASE_H, 2.5)
    w, h = round(BASE_W * s), round(BASE_H * s)
    assert w <= MON1[2] - 2 * asc.MONITOR_MARGIN
    assert h <= MON1[3] - 2 * asc.MONITOR_MARGIN
    assert s < 2.5   # a 1.25-scale 2560 monitor cannot show the full band
    assert asc.effective_scale(MON1, BASE_W, BASE_H, 1.0) == 1.0
    # headroom windows raise the ceiling; the monitor itself is the wall
    big = asc.effective_scale(MON1, BASE_W, BASE_H, 4.0, max_scale=asc.WINDOW_SCALE_MAX)
    assert big >= s and big <= min(MON1[2] / BASE_W, MON1[3] / BASE_H)
    # avail_h: growth above an anchored feet line caps tighter than the
    # whole monitor (feet anchoring beats filling the screen)
    tight = asc.effective_scale(MON1, BASE_W, BASE_H, 2.5, avail_h=340)
    assert tight * BASE_H <= 340
    # junk scale -> 1.0-equivalent size, tiny monitor stays positive
    assert asc.effective_scale(MON1, BASE_W, BASE_H, "junk") == 1.0
    assert asc.effective_scale((0, 0, 100, 100), BASE_W, BASE_H, 2.0) >= 0.01


# ── real 0.56.2 monitor rows ──────────────────────────────────────────

def test_monitor_logical_rect_real_rows():
    # DP-2: 2560x1440 @ scale 1, 44 px top bar reserved
    assert asc.monitor_logical_rect(REAL_MONITORS[0]) == (0, 44, 2560, 1396)
    # HDMI-A-1: PHYSICAL 2560x1440 at scale 1.25 → logical 2048x1152 minus
    # the bar; x/y are already logical (5120 = two scale-1 monitors to the
    # left). This logical-vs-physical trap made the owner's zoom stop at 1.3.
    assert MON1 == (5120, 44, 2048, 1108)


def test_monitor_logical_rect_scale_transform_reserved():
    base = {"id": 0, "width": 1920, "height": 1080, "x": 0, "y": 0,
            "scale": 1, "transform": 0, "reserved": [0, 0, 0, 0]}
    assert asc.monitor_logical_rect(base) == (0, 0, 1920, 1080)
    s = dict(base, scale=1.25, x=2000, y=-500)
    assert asc.monitor_logical_rect(s) == (2000, -500, 1536, 864)
    assert asc.monitor_logical_rect(dict(base, transform=1)) == (0, 0, 1080, 1920)
    assert asc.monitor_logical_rect(dict(base, transform=2)) == (0, 0, 1920, 1080)
    assert asc.monitor_logical_rect(dict(base, reserved=[10, 40, 20, 0])) == (10, 40, 1890, 1040)
    assert asc.monitor_logical_rect(dict(base, disabled=True)) is None
    # a non-list `reserved` is ignored (tolerant), not fatal
    assert asc.monitor_logical_rect(dict(base, reserved="junk")) == (0, 0, 1920, 1080)
    for junk in (None, {}, [], dict(base, width=0), dict(base, scale=0),
                 dict(base, scale="x"), dict(base, reserved=[0, 1080, 0, 0])):
        assert asc.monitor_logical_rect(junk) is None


def test_monitor_rect_for_matches_client_monitor_id():
    assert asc.monitor_rect_for(OWN, REAL_MONITORS) == MON1
    by_id = [{"id": 7, "width": 800, "height": 600, "x": 0, "y": 0,
              "scale": 1, "transform": 0, "reserved": [0, 0, 0, 0]}]
    assert asc.monitor_rect_for({"monitor": 7, "at": [1, 1], "size": [10, 10]}, by_id) == (0, 0, 800, 600)


def test_monitor_rect_for_falls_back_to_containment_and_overlap():
    mons = [
        {"id": 0, "width": 1000, "height": 1000, "x": 0, "y": 0, "scale": 1,
         "transform": 0, "reserved": [0, 0, 0, 0]},
        {"id": 1, "width": 1000, "height": 1000, "x": 1000, "y": 0, "scale": 1,
         "transform": 0, "reserved": [0, 0, 0, 0]},
        {"id": 2, "width": 500, "height": 500, "x": 2000, "y": 0, "scale": 1,
         "transform": 0, "reserved": [0, 0, 0, 0], "disabled": True},
    ]
    # monitor -1/missing: centre inside the right monitor
    assert asc.monitor_rect_for({"at": [1400, 100], "size": [10, 10]}, mons) == (1000, 0, 1000, 1000)
    # straddling with more coverage on the left: centre decides (tie area → largest overlap)
    assert asc.monitor_rect_for({"monitor": -1, "at": [900, 100], "size": [10, 10]}, mons) == (0, 0, 1000, 1000)
    # fully off all monitors: biggest overlap wins (zero here -> None)
    assert asc.monitor_rect_for({"at": [9000, 9000], "size": [10, 10]}, mons) is None
    # disabled monitor id (2): the id never matches, so the centre-containment
    # fallback decides — the window physically lives on the left monitor
    assert asc.monitor_rect_for({"monitor": 2, "at": [0, 0], "size": [10, 10]}, mons) == (0, 0, 1000, 1000)
    assert asc.monitor_rect_for(None, mons) is None
    assert asc.monitor_rect_for({"monitor": 0}, []) is None


def test_monitor_rect_for_survives_garbage_rows():
    # a future Hyprland schema with unexpected rows must not explode
    assert asc.monitor_rect_for(OWN, [None, "junk", 5]) is None
    assert asc.monitor_rect_for({"monitor": "x", "at": None, "size": None}, REAL_MONITORS) is None


# ── feet-anchored geometry ────────────────────────────────────────────

def test_grow_anchors_feet_centre():
    mon = (0, 0, 2560, 2400)                           # room for a 2x window
    cur = (1000, 700, BASE_W, BASE_H)
    x, y, w, h = asc.scaled_geometry(mon, cur, BASE_W, BASE_H, 2.0)
    assert (w, h) == (round(BASE_W * 2), round(BASE_H * 2))
    assert y + h == cur[1] + cur[3]                    # bottom edge never moves
    assert abs((x + w / 2) - (cur[0] + cur[2] / 2)) <= 1  # horizontal centre too


def test_shrink_anchors_feet_centre():
    mon = (0, 0, 2560, 2400)
    cur = (1000, 700, BASE_W * 2, BASE_H * 2)
    x, y, w, h = asc.scaled_geometry(mon, cur, BASE_W, BASE_H, 1.0)
    assert (w, h) == (BASE_W, BASE_H)
    assert y + h == cur[1] + cur[3]
    assert abs((x + w / 2) - (cur[0] + cur[2] / 2)) <= 1


def test_headroom_then_fit_lands_back_on_the_start_rect():
    # the exact gesture the page runs: fit at 1.0 → headroom grow → fit at
    # a bigger final scale; feet anchoring is what makes the moves invisible
    start = (6000, 300, BASE_W, BASE_H)
    g1 = asc.scaled_geometry(MON1, start, BASE_W, BASE_H, 1.0)
    assert g1 == start
    grow = asc.scaled_geometry(MON1, g1, BASE_W, BASE_H, 1.4 * 1.6)
    fit = asc.scaled_geometry(MON1, grow, BASE_W, BASE_H, 1.4)
    assert grow[1] + grow[3] == g1[1] + g1[3]          # grow anchors the feet
    assert grow[3] > g1[3]
    assert fit[1] + fit[3] == grow[1] + grow[3]        # fit anchors them again
    assert fit[3] == grow[3]                           # 1.4 exceeds this
    #   monitor's growth room, so both clamp at the same height (feet first!)
    assert fit[1] + fit[3] == start[1] + start[3]


def test_growth_stops_instead_of_moving_the_feet():
    # window whose feet are low on the monitor: growth is capped to the
    # space ABOVE the feet; the bottom edge NEVER slides down (y-clamping
    # the top instead would = exactly the jerky jump this design kills)
    mon = (0, 0, 1920, 1080)
    cur = (700, 1080 - BASE_H, BASE_W, BASE_H)         # bottom at the monitor bottom
    g = asc.scaled_geometry(mon, cur, BASE_W, BASE_H, 3.0)
    assert g[1] + g[3] == cur[1] + cur[3]              # feet never move
    assert g[1] >= 0                                   # top stays on-screen
    assert g[3] <= 1080


def test_geometry_clamped_fully_inside_monitor():
    # window near the left edge growing: gets pushed right, never off-screen
    cur = (1010, 300, BASE_W, BASE_H)
    x, y, w, h = asc.scaled_geometry(MON1, cur, BASE_W, BASE_H, 1.5)
    assert x >= MON1[0] and x + w <= MON1[0] + MON1[2]
    assert y >= MON1[1] and y + h <= MON1[1] + MON1[3]
    # window at the very bottom growing: y sticks to the top, never below
    cur = (6000, MON1[1] + MON1[3] - BASE_H, BASE_W, BASE_H)
    x, y, w, h = asc.scaled_geometry(MON1, cur, BASE_W, BASE_H, 1.4)
    assert y + h <= MON1[1] + MON1[3]
    assert y >= MON1[1]
    # a monitor with an odd offset (owner layout: x=5120, y=-500 style)
    off = (5120, -500, MON1[2], MON1[3])
    cur = (6000, -100, BASE_W, BASE_H)
    x, y, w, h = asc.scaled_geometry(off, cur, BASE_W, BASE_H, 1.3)
    assert x >= 5120 and x + w <= 5120 + MON1[2]
    assert y >= -500 and y + h <= -500 + MON1[3]


# ── compositor commands ───────────────────────────────────────────────

def test_lua_dispatches_target_own_window_only():
    out = asc.hypr_scale_dispatches("lua", 840, 1360, 700, 120, "0xabc", "riko")
    assert len(out) == 2
    assert "address:0xabc" in out[0] and "address:0xabc" in out[1]
    assert "x=840, y=1360" in out[0]        # resize FIRST (feet anchor math
    assert "x=700, y=120" in out[1]         #  assumes the final size)


def test_legacy_dispatches_quote_own_class():
    out = asc.hypr_scale_dispatches("legacy", 840, 1360, 700, 120, "0xabc")
    assert out[0] == '"resizewindowpixel exact 840 1360,class:^(riko)$"'
    assert out[1] == '"movewindowpixel exact 700 120,class:^(riko)$"'


def test_batch_is_one_request_split_on_semicolons():
    # hyprctl v0.56.2 source: --batch sends the rest of the argv as ONE
    # [[BATCH]] request; the server splits on ';' OUTSIDE brackets and runs
    # each part through the normal dispatch path — so exactly two parts.
    argv = asc.hypr_scale_batch("lua", 840, 1360, 700, 120, "0xabc")
    assert argv[0] == "hyprctl" and argv[1] == "--batch"
    parts = argv[2].split(" ; ")
    assert len(parts) == 2
    assert parts[0].startswith("dispatch hl.dsp.window.resize(")
    assert parts[1].startswith("dispatch hl.dsp.window.move(")
    assert "address:0xabc" in argv[2]
    assert "[" not in argv[2]               # no bracket that could eat a split


def test_batch_reply_ok():
    assert asc.batch_reply_ok("ok\n\n\nok") == (True, "")
    ok, err = asc.batch_reply_ok("ok\n\n\nunknown dispatcher")
    assert ok is False and err == "unknown dispatcher"
    assert asc.batch_reply_ok("ok") == (False, "ok")      # missing 2nd reply
    assert asc.batch_reply_ok("") == (False, "empty reply")


# ── the sizer: fake hyprctl, no compositor, no window ─────────────────

def own_row(at, size, addr="0x5642c7cbeba0", monitor=1):
    row = dict(OWN, at=list(at), size=list(size), address=addr, monitor=monitor)
    other = dict(OWN, title="Other", address="0xdeadbeef")
    other["class"] = "kitty"          # 'class' is a keyword: set via item
    return json.dumps([other, row])

class FakeHypr:
    """Fake hyprctl: clients/monitors answer with the owner's real rows,
    --batch records the dispatch and (like v0.56.2) replies 'ok' per part."""

    def __init__(self, at, size, addr="0x5642c7cbeba0", style="lua", fail_batch=False,
                 monitor=1):
        self.at, self.size = list(at), list(size)
        self.addr = addr
        self.style = style
        self.fail_batch = fail_batch
        self.monitor = monitor
        self.dispatches = []          # every --batch argv
        self.listing_calls = 0        # -j clients/monitors round trips

    def __call__(self, argv, timeout=None, **kw):
        return self._one(list(argv))

    def _one(self, argv):
        class R:
            returncode, stderr = 0, ""
        if argv[:2] == ["hyprctl", "--batch"]:
            self.dispatches.append(argv)
            R.stdout = "\n\n\n".join(["bad dispatch"] if self.fail_batch else ["ok"] * 2)
            return R
        if argv[:2] == ["hyprctl", "-j"]:
            argv = ["hyprctl", argv[2], "-j"]     # accept both arg orders
        if argv[:2] == ["hyprctl", "clients"]:
            self.listing_calls += 1
            R.stdout = own_row(self.at, self.size, self.addr, self.monitor)
            return R
        if argv[:2] == ["hyprctl", "monitors"]:
            self.listing_calls += 1
            R.stdout = json.dumps(REAL_MONITORS)
            return R
        if argv == ["hyprctl", "dispatch", "hl.dsp.no_op()"]:
            R.stdout = "ok"          # the ambiguous-config probe
            return R
        raise AssertionError(f"unexpected argv {argv}")

    def apply_batch(self, w, h, x, y):
        # apply the batch like the compositor would: the LAST dispatch wins
        for part in self.dispatches[-1][2].split(" ; "):
            if "window.resize" in part:
                self.size = [w, h]
            elif "window.move" in part:
                self.at = [x, y]
            elif "resizewindowpixel" in part:
                self.size = [w, h]
            elif "movewindowpixel" in part:
                self.at = [x, y]


def sizer(runner=None, at=(6675, 227), size=(BASE_W, BASE_H), style="lua", **kw):
    fake = runner if isinstance(runner, FakeHypr) else FakeHypr(at, size, **kw)
    return asc.HyprlandSizer(BASE_W, BASE_H, "riko", runner=fake, style=style), fake


def test_sizer_apply_is_one_batch_and_feet_anchor():
    # DP-3 (id 2, scale 1) placed high enough that 1.5x growth room exists
    sz, fake = sizer(at=(3000, 500), size=(BASE_W, BASE_H), monitor=2)
    res = sz.apply(1.5)
    assert res["ok"]
    assert len(fake.dispatches) == 1                    # ONE compositor request
    assert fake.dispatches[0][:2] == ["hyprctl", "--batch"]
    assert len(fake.dispatches[0]) == 3                 # argv is exactly hyprctl --batch <one string>
    x, y, w, h = res["geometry"]
    assert (w, h) == (round(BASE_W * 1.5), round(BASE_H * 1.5))
    assert y + h == 500 + BASE_H                        # feet anchored
    assert abs((x + w / 2) - (3000 + BASE_W / 2)) <= 1
    assert f"x={w}, y={h}" in fake.dispatches[0][2]     # batch carries the geometry
    assert f"x={x}, y={y}" in fake.dispatches[0][2]
    assert abs(res["scale"] - 1.5) < 1e-9


def test_sizer_monitors_and_window_are_cached():
    sz, fake = sizer(at=(3000, 500), monitor=2)
    sz.apply(1.2)
    n = fake.listing_calls
    assert n >= 2                                       # first apply reads clients+monitors
    sz.apply(1.3)
    assert fake.listing_calls == n                      # grow+fit reuse the cache
    assert len(fake.dispatches) == 2                    # but each apply dispatches


def test_sizer_monitor_cache_expires():
    t = [1000.0]
    fake = FakeHypr((3000, 500), (BASE_W, BASE_H), monitor=2)
    s2 = asc.HyprlandSizer(BASE_W, BASE_H, "riko", runner=fake, style="lua",
                           clock=lambda: t[0])
    s2.apply(1.1)
    n = fake.listing_calls
    t[0] += asc.MONITOR_CACHE_TTL + 1                   # display config may have changed
    s2.apply(1.2)
    assert fake.listing_calls > n


def test_sizer_grow_then_fit_lands_on_the_start_rect():
    # the invisible-fit contract, desktop side: grow to headroom, fit to
    # final — the fit rect has the bottom edge back exactly where the
    # gesture started it, so her feet never moved on screen
    sz, fake = sizer()
    g1 = sz.apply(1.0)["geometry"]
    assert (g1[2], g1[3]) == (BASE_W, BASE_H)
    grow = sz.apply(1.4 * 1.6)["geometry"]
    assert grow[1] + grow[3] == g1[1] + g1[3]           # grow anchors the feet
    assert grow[3] > g1[3]
    fit = sz.apply(1.4)["geometry"]
    assert fit[1] + fit[3] == grow[1] + grow[3]         # fit anchors them again
    assert fit[3] == grow[3]      # 1.4 > this monitor's growth room (863 px
    #                             above her feet): grow and fit clamp equal,
    #                             the FEET, not the fill, decide the ceiling
    assert len(fake.dispatches) == 3                    # grow + fit = ONE batch each


def test_sizer_reports_fit_cap_and_clamps_growth():
    sz, fake = sizer()
    res = sz.apply(2.5)                                 # band max on the 1.25 monitor
    assert res["ok"]
    assert res["max"] < 2.5                             # monitor says stop here
    w, h = res["geometry"][2], res["geometry"][3]
    assert h <= MON1[3] - 2 * asc.MONITOR_MARGIN
    assert abs(res["scale"] - res["max"]) < 1e-6        # applied == the cap
    # headroom windows can't buy pixels the monitor doesn't have: both the
    # final cap and the headroom clamp meet at the growth room (feet first)
    big = sz.apply(1.9 * 1.6)
    assert big["ok"] and big["scale"] >= res["max"]
    assert big["geometry"][1] + big["geometry"][3] == res["geometry"][1] + res["geometry"][3]
    # the cap is a float, never a crash: a window taller than the monitor
    tiny = asc.HyprlandSizer(3000, 2000, "riko", runner=FakeHypr([0, 0], [3000, 2000]), style="lua")
    cap = tiny.fit_cap()
    assert 0.01 <= cap < 1.0


def test_sizer_refreshes_after_dispatch_failure():
    fake = FakeHypr((6675, 227), (BASE_W, BASE_H), fail_batch=True)
    sz, _ = sizer(runner=fake)
    res = sz.apply(1.2)
    assert not res["ok"] and "bad dispatch" in res["error"]
    n_after_fail = fake.listing_calls                   # caches were dropped
    assert n_after_fail >= 2
    fake.fail_batch = False
    res = sz.apply(1.3)
    assert res["ok"]
    assert fake.listing_calls >= n_after_fail + 2       # state re-read before dispatch


def test_sizer_missing_own_window_is_error_not_crash():
    sz = asc.HyprlandSizer(BASE_W, BASE_H, "riko",
                           runner=lambda argv, timeout=None, **kw: _r("[]"),
                           style="lua")
    res = sz.apply(1.0)
    assert not res["ok"] and "window" in res["error"].lower()
    sz = asc.HyprlandSizer(BASE_W, BASE_H, "riko",
                           runner=lambda argv, timeout=None, **kw: _r("not json"),
                           style="lua")
    res = sz.apply(1.0)
    assert not res["ok"] and "hyprctl clients failed" in res["error"]


def test_sizer_legacy_style_selects_class():
    sz, fake = sizer(style="legacy")
    res = sz.apply(1.5)
    assert res["ok"]
    assert "class:^(riko)$" in fake.dispatches[-1][2]
    assert "hl.dsp" not in fake.dispatches[-1][2]


def _r(out):
    class R:
        returncode, stderr, stdout = 0, "", out
    return R


# ── constants the page and desktop must agree on ──────────────────────

def test_constants_match_the_page():
    assert asc.SCALE_MIN == 0.4 and asc.SCALE_MAX == 2.5
    assert asc.SCALE_STEP == 1.08
    assert asc.MONITOR_CACHE_TTL == 30.0
    assert math.isfinite(asc.WINDOW_SCALE_MAX)
