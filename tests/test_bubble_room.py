"""Tests for riko/bubble_room.py — grow her window upward while she talks.

Owner decision (docs/TOUCH.md "Speech bubble headroom"): the speech bubble
goes ABOVE her head, so while it is shown the window grows by exactly the
missing px (y -= extra, h += extra, x/w unchanged, feet and her size
untouched) and shrinks back after the bubble has been gone for a while.

Covered: the page-mirrored arithmetic (room_needed), the exact grow/release
rects (feet-anchored, monitor-top clamp, partial grant), the grow/keep/
release state machine (keep-alive is a no-op, grow-further, no-room, release
restores the pre-grow rect EXACTLY, drag while grown keeps the user's
position), and the IO wrapper's ONE batched hyprctl argv (exact argv,
no-op dispatches, failure handling, no persistence). Fake hyprctl runner
only — the owner's live session is never queried.
"""
import inspect
import json
import os

from test_avatar_scale import BASE_H, BASE_W, MON1, FakeHypr, OWN
import riko.bubble_room as br
from riko import avatar_scale as asc

# her window high on the monitor (bottom 1080 = 1036 px of growth room, so a
# MAX_EXTRA ask grants in full) vs low on it (bottom 907 → 863 px → a 220 px
# ask can only grant 183)
HIGH = (6675, 400, BASE_W, BASE_H)
LOW = (6675, 227, BASE_W, BASE_H)
BIG_MON = (6000, 0, 2560, 1440)


def _room(at, size, monitor=None, **kw):
    """HyprlandBubbleRoom over the shared scroll-zoom sizer + fake hyprctl."""
    fake = FakeHypr(at, size, monitor=kw.pop("monitor", 1), **kw)
    sz = asc.HyprlandSizer(BASE_W, BASE_H, "riko", runner=fake, style="lua", **kw)
    return br.HyprlandBubbleRoom(sz), fake


# ── the arithmetic the page runs too ──────────────────────────────────

def test_room_needed_matches_the_above_slot():
    # 'above' has (headTopY - gap - margin) px of space: exactly enough = 0
    assert br.room_needed(head_top_y=104, bubble_h=88) == 0     # 104-8-8 = 88
    assert br.room_needed(head_top_y=44, bubble_h=61) == 48     # 33 -> 48
    assert br.room_needed(head_top_y=24, bubble_h=61) == 64     # owner rig
    # step rounding (16 px) and the 220 px ceiling
    assert br.room_needed(44, 89) == 64                          # 61 → 64
    assert br.room_needed(44, 400) == br.MAX_EXTRA
    assert br.room_needed(44, 400, max_extra=96) == 96
    for junk in (None, "", "abc", float("nan"), float("inf")):
        assert br.room_needed(junk, 100) == 0
        assert br.room_needed(44, junk) == 0
    assert br.room_needed(44, 8) == 0        # nothing to gain


def test_round_step_rounds_up_and_ignores_junk():
    assert br.round_step(1) == 16
    assert br.round_step(16) == 16
    assert br.round_step(17) == 32
    assert br.round_step(0) == 0
    assert br.round_step(-5) == 0
    assert br.round_step(float("nan")) == 0


# ── exact geometry ────────────────────────────────────────────────────

def test_grow_target_is_y_minus_extra_h_plus_extra():
    granted, (x, y, w, h) = br.grow_target(HIGH, BASE_W, BASE_H, 96, BIG_MON)
    assert granted == 96
    assert (x, w) == (HIGH[0], BASE_W)          # x and width untouched
    assert h == BASE_H + 96
    assert y == HIGH[1] - 96                    # y -= extra
    assert y + h == HIGH[1] + HIGH[3]           # bottom (her feet) unmoved


def test_grow_target_clamps_to_monitor_top_and_reports_granted():
    mon = (0, 44, 2560, 1396)                   # a 44 px reserved bar
    cur = (100, 200, BASE_W, BASE_H)            # feet line at 880
    granted, geom = br.grow_target(cur, BASE_W, BASE_H, 220, mon)
    assert granted == 880 - 44 - BASE_H         # only what fits above the bar
    assert geom[1] == 44                        # top edge exactly at the bar
    assert geom[3] == BASE_H + granted
    assert geom[1] + geom[3] == 880            # feet line untouched
    # nothing above the head at all → granted 0, geometry unchanged
    at_top = (100, 44, BASE_W, BASE_H)
    g0, geom0 = br.grow_target(at_top, BASE_W, BASE_H, 96, mon)
    assert g0 == 0 and geom0[3] == BASE_H
    # junk / negative asks grow nothing
    assert br.grow_target(cur, BASE_W, BASE_H, "x", mon)[0] == 0
    assert br.grow_target(cur, BASE_W, BASE_H, -40, mon)[0] == 0
    # asked more than the cap: the cap is what grows
    assert br.grow_target(cur, BASE_W, BASE_H, 5000, mon)[0] <= br.MAX_EXTRA


def test_grow_target_keeps_her_on_screen_horizontally():
    mon = (0, 0, 1000, 900)
    granted, (x, y, w, h) = br.grow_target((980, 100, BASE_W, BASE_H),
                                           BASE_W, BASE_H, 64, mon)
    assert granted == 64
    assert x + w <= 1000 and x >= 0


def test_release_target_is_exact_without_drag_and_follows_a_drag():
    _, grown = br.grow_target(HIGH, BASE_W, BASE_H, 96, BIG_MON)
    # nobody moved her: the release rect IS the pre-grow rect, pixel exact
    assert br.release_target(grown, BASE_W, BASE_H, BIG_MON) == HIGH
    # she was dragged (dx, dy): the feet stay where the user put them
    dragged = (grown[0] + 137, grown[1] - 53, grown[2], grown[3])
    back = br.release_target(dragged, BASE_W, BASE_H, BIG_MON)
    assert (back[2], back[3]) == (BASE_W, BASE_H)     # normal size restored
    assert back[0] == HIGH[0] + 137                   # user's x kept
    assert back[1] + BASE_H == dragged[1] + dragged[3]  # user's feet line kept


# ── the state machine ─────────────────────────────────────────────────

def test_state_grow_keep_grow_further_release():
    st = br.BubbleRoom()
    d1 = st.want(64, LOW, BIG_MON)
    assert d1["action"] == "grow" and d1["granted"] == 64
    assert st.saved == LOW and st.base == (BASE_W, BASE_H)
    assert st.grown and st.granted == 64
    st.applied(d1["geometry"])
    # the page's keep-alive re-ask (same number, window already grown):
    # 'already' = no dispatch, so one bubble = one grow
    d2 = st.want(64, d1["geometry"], BIG_MON)
    assert d2["action"] == "already" and d2["granted"] == 64
    # a longer reply while shown: grows FURTHER from the grown rect, never
    # below the feet line
    st.applied(d2["geometry"])
    d3 = st.want(112, d1["geometry"], BIG_MON)
    assert d3["action"] == "grow" and d3["granted"] == 112
    assert d3["geometry"][3] == BASE_H + 112
    assert d3["geometry"][1] + d3["geometry"][3] == LOW[1] + LOW[3]
    # never shrinks until release
    assert st.want(32, d3["geometry"], BIG_MON)["granted"] == 112
    # release: the pre-grow rect exactly
    r = st.released(d3["geometry"], BIG_MON)
    assert r["action"] == "release" and r["geometry"] == LOW and r["granted"] == 0


def test_state_release_when_never_grown_is_a_noop():
    st = br.BubbleRoom()
    r = st.released(LOW, BIG_MON)
    assert r["action"] == "idle" and r["geometry"] is None
    assert st.grown is False


def test_state_no_room_keeps_the_window_and_pages_stays_on_body():
    mon = (0, 44, 2560, 1396)
    st = br.BubbleRoom()
    d = st.want(96, (100, 44, BASE_W, BASE_H), mon)    # glued to the bar
    assert d["action"] == "no-room" and d["granted"] == 0 and d["geometry"] is None
    assert st.grown is False and st.saved is None
    # partial grant is what the page is told (feet line 880, usable top 44)
    d2 = st.want(220, (100, 200, BASE_W, BASE_H), mon)
    assert d2["action"] == "grow" and 0 < d2["granted"] < 220


def test_state_failed_dispatch_reapplies_next_time():
    st = br.BubbleRoom()
    d1 = st.want(64, LOW, BIG_MON)
    st.applied(d1["geometry"])
    st.failed()
    assert st.geometry is None
    assert st.want(64, d1["geometry"], BIG_MON)["action"] == "grow"


def test_state_release_after_an_external_resize_restores_normal_size():
    # a scale FIT shrank the window mid-bubble: release still lands on the
    # normal size at the current feet line (no stale grown height)
    st = br.BubbleRoom()
    d = st.want(64, LOW, BIG_MON)
    st.applied(d["geometry"])
    shrunk = (LOW[0], LOW[1] + 64, BASE_W, BASE_H)
    r = st.released(shrunk, BIG_MON)
    assert r["action"] == "release"
    assert (r["geometry"][2], r["geometry"][3]) == (BASE_W, BASE_H)
    assert r["geometry"][1] + BASE_H == shrunk[1] + shrunk[3]


def test_state_reset_forgets_without_dispatching():
    st = br.BubbleRoom()
    st.want(64, LOW, BIG_MON)
    st.reset()
    assert st.grown is False and st.base is None and st.saved is None
    assert st.released(LOW, BIG_MON)["action"] == "idle"


# ── the IO wrapper: exact hyprctl argv, one batch, no persistence ─────

def test_hypr_grow_argv_is_one_batched_call():
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    res = room.apply(96)
    assert res["ok"] and res["granted"] == 96 and res["reason"] == "grown"
    assert len(fake.dispatches) == 1                      # ONE compositor request
    x, y, w, h = res["geometry"]
    assert (w, h) == (BASE_W, BASE_H + 96)
    assert y + h == HIGH[1] + BASE_H                      # feet anchored
    addr = OWN["address"]
    assert fake.dispatches[0] == asc.hypr_scale_batch("lua", w, h, x, y, addr)
    assert fake.dispatches[0][:2] == ["hyprctl", "--batch"]
    body = fake.dispatches[0][2]
    assert f"hl.dsp.window.resize({{x={w}, y={h}, window=\"address:{addr}\"}})" in body
    assert f"hl.dsp.window.move({{x={x}, y={y}, window=\"address:{addr}\"}})" in body
    assert body.count(" ; ") == 1                         # resize ; move, one request
    # the exact argv the release takes (asserted literally, not just run):
    rel = room.release()
    assert rel["ok"] and rel["granted"] == 0 and rel["reason"] == "released"
    assert len(fake.dispatches) == 2
    assert fake.dispatches[1] == asc.hypr_scale_batch("lua", BASE_W, BASE_H,
                                                      HIGH[0], HIGH[1], addr)
    resize, move = fake.dispatches[1][2].split(" ; ")
    assert resize == f'dispatch hl.dsp.window.resize({{x={BASE_W}, y={BASE_H}, ' \
                     f'window="address:{addr}"}})'
    assert move == f'dispatch hl.dsp.window.move({{x={HIGH[0]}, y={HIGH[1]}, ' \
                   f'window="address:{addr}"}})'   # y back exactly where it was


def test_hypr_release_without_grow_never_dispatches():
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    res = room.release()
    assert res["ok"] and res["granted"] == 0 and res["reason"] == "idle"
    assert fake.dispatches == []
    # an ask of 0 (release) with nothing grown is the same no-op
    assert room.apply(0)["reason"] == "idle"
    assert fake.dispatches == []


def test_hypr_keepalive_reask_does_not_touch_the_compositor():
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    room.apply(64)
    for _ in range(5):                      # the page re-asks while she talks
        res = room.apply(64)
        assert res["ok"] and res["granted"] == 64 and res["reason"] == "already"
    assert len(fake.dispatches) == 1
    # a longer line does grow (one more dispatch), still one request each
    assert room.apply(96)["reason"] == "grown"
    assert len(fake.dispatches) == 2
    # one release per bubble: a second release is a no-op
    assert room.release()["reason"] == "released"
    assert room.release()["reason"] == "idle"
    assert len(fake.dispatches) == 3


def test_hypr_monitor_top_grants_only_what_fits():
    # LOW on the owner's real monitor MON1: feet line 907, usable top 44 →
    # 863 - 680 = 183 px of room, so a 220 px ask grants 183
    room, fake = _room((LOW[0], LOW[1]), (BASE_W, BASE_H), monitor=1)
    assert MON1 == (5120, 44, 2048, 1108)
    res = room.apply(br.MAX_EXTRA)
    assert res["ok"] and res["granted"] == 907 - 44 - BASE_H
    x, y, w, h = res["geometry"]
    assert y >= MON1[1] and y + h == 907 and w == BASE_W
    # glued to the bar: nothing to give, and no dispatch at all
    room2, fake2 = _room((LOW[0], 44), (BASE_W, BASE_H), monitor=1)
    res2 = room2.apply(96)
    assert res2["ok"] and res2["granted"] == 0 and res2["reason"] == "no-room"
    assert fake2.dispatches == []


def test_hypr_drag_while_grown_releases_where_the_user_left_her():
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    grown = room.apply(96)["geometry"]
    fake.apply_batch(grown[2], grown[3], grown[0], grown[1])   # compositor applies
    # the user drags her: same grown size, new position
    fake.at = [grown[0] + 40, grown[1] + 30]   # still inside the monitor
    keep = room.apply(96)                     # keep-alive must not fight the drag
    assert keep["reason"] == "already"
    assert len(fake.dispatches) == 1
    room.mark_dirty()                          # desktop.py's drag-end signal
    assert room.apply(96)["reason"] == "already"
    assert len(fake.dispatches) == 1           # re-read, still no window move
    rel = room.release()
    assert rel["ok"] and rel["reason"] == "released"
    x, y, w, h = rel["geometry"]
    assert (w, h) == (BASE_W, BASE_H)                  # her normal window back
    assert x == HIGH[0] + 40                           # where she dragged her
    assert y + h == grown[1] + 30 + grown[3]           # feet stay there too


def test_hypr_external_resize_is_repaired_by_the_next_ask():
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    grown = room.apply(96)["geometry"]
    fake.apply_batch(grown[2], grown[3], grown[0], grown[1])
    # a scroll-zoom FIT shrinks the window back mid-bubble (same feet line)
    fake.size = [BASE_W, BASE_H]
    fake.at = [HIGH[0], HIGH[1]]
    room.state.geometry = (HIGH[0], HIGH[1], BASE_W, BASE_H)   # what we can see
    room.state.failed()                                        # ...if we noticed
    res = room.apply(96)
    assert res["ok"] and res["granted"] == 96 and res["reason"] == "grown"
    assert res["geometry"][3] == BASE_H + 96
    assert res["geometry"][1] + res["geometry"][3] == HIGH[1] + BASE_H


def test_hypr_dispatch_failure_reports_and_recovers():
    room, fake = _room_fail()
    res = room.apply(64)
    assert res["ok"] is False and res["reason"] == "dispatch-failed"
    assert res["error"] and "argv" in res
    before = fake.listing_calls
    room2 = br.HyprlandBubbleRoom(room.sizer)   # fresh state, same fake
    assert room2.apply(64)["ok"] is False       # still failing, still answering
    assert fake.listing_calls > before          # state refreshed after failure


def test_hypr_no_sizer_and_no_window_answer_cleanly():
    room = br.HyprlandBubbleRoom(None)
    assert room.apply(64)["reason"] == "no-desktop"
    assert room.release()["ok"] is True

    def no_windows(argv, timeout=None, **kw):
        class R:
            returncode, stderr, stdout = 0, "", "[]"     # no windows at all
        return R()

    sz = asc.HyprlandSizer(BASE_W, BASE_H, "riko", runner=no_windows, style="lua")
    res = br.HyprlandBubbleRoom(sz).apply(64)
    assert res["ok"] is False and res["reason"] == "no-window"
    assert "argv" not in res and res["granted"] == 0


def test_hypr_abandon_lets_the_strip_take_the_window():
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    room.apply(64)
    room.abandon()                              # roam strip takeover
    assert room.granted == 0
    assert room.release()["reason"] == "idle"   # no stale restore of the strip
    assert len(fake.dispatches) == 1


def _room_fail():
    """HyprlandBubbleRoom whose every --batch replies 'bad dispatch'."""
    fake = FakeHypr((HIGH[0], HIGH[1]), [BASE_W, BASE_H], fail_batch=True)
    sz = asc.HyprlandSizer(BASE_W, BASE_H, "riko", runner=fake, style="lua")
    return br.HyprlandBubbleRoom(sz), fake


def test_bubble_room_never_persists_geometry():
    src = inspect.getsource(br).replace(br.__doc__ or "", "")
    src += inspect.getsource(asc.HyprlandSizer.dispatch_geometry)
    src += inspect.getsource(asc.HyprlandSizer.window_state)
    for forbidden in ("appstate", "save_scale", "json.dump", "open("):
        assert forbidden not in src, f"bubble room must not persist anything ({forbidden})"


def test_sizer_state_is_shared_not_re_read_per_call():
    # window_state() is cached: grow + keep-alive + release = ONE -j pair
    room, fake = _room((HIGH[0], HIGH[1]), (BASE_W, BASE_H))
    room.apply(64)
    room.apply(64)
    room.release()
    # grow + keep-alive share ONE -j pair; the release forces a fresh read
    # (a drag while grown must release where the user left her), so two
    # pairs total — never one per request.
    assert fake.listing_calls == 4


# ── a zoom FIT landing while the bubble is up ────────────────────────
def test_rebase_re_measures_the_room_after_a_fit():
    """The scroll-zoom FIT resizes the window to base x fit, which drops the
    grown band. The room must then be measured from the NEW normal size (same
    px of room, feet line untouched), not from the pre-grow 420x680."""
    st = br.BubbleRoom()
    mon = (0, 44, 2560, 1396)
    d1 = st.want(96, (100, 400, BASE_W, BASE_H), mon)
    st.applied(d1["geometry"])                       # grown: 420x776
    assert d1["geometry"] == (100, 400 - 96, BASE_W, BASE_H + 96)
    # the wheel's fit lands (fit 1.25 -> 525x850, feet-anchored like every
    # avatar_scale apply): the room rebases instead of fighting it
    fit = (100, 1080 - 850, 525, 850)
    st.rebase(525, 850)
    assert st.geometry is None                       # nothing is trusted now
    d2 = st.want(96, fit, mon)
    assert d2["action"] == "grow" and d2["granted"] == 96
    x, y, w, h = d2["geometry"]
    assert (w, h) == (525, 850 + 96)                 # new size + the room
    assert y + h == 1080                             # her feet never moved
    # not grown => nothing to rebase (a plain FIT is left completely alone)
    st2 = br.BubbleRoom()
    st2.rebase(525, 850)
    assert st2.base is None and st2.granted == 0


def test_hypr_rebase_forces_the_next_ask_to_redispatch():
    grown_h = BASE_H + 96
    room, fake = _room((HIGH[0], 400 + 96), [BASE_W, grown_h])
    assert room.apply(96)["reason"] == "grown"       # sizer reports the grown
    n = len(fake.dispatches)
    assert room.apply(96)["reason"] == "already"     # keep-alive: no dispatch
    assert len(fake.dispatches) == n
    room.rebase(525, 850)                            # a FIT changed her size
    out = room.apply(96)
    assert out["reason"] == "grown" and out["granted"] == 96
    assert len(fake.dispatches) == n + 1
    body = fake.dispatches[-1][2]
    resize, move = body.split(" ; ")
    assert f"y={850 + 96}" in resize                 # resize to the new height
    assert f"x={525}" in resize
    assert f"y={400 + 96 + grown_h - (850 + 96)}" in move   # feet line kept
