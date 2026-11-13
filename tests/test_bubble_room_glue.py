"""desktop.py's /control/bubble-room route (docs/TOUCH.md "Speech bubble
headroom"): the guards that live OUTSIDE the pure geometry (roam strip, hidden
window, scale gesture in flight), the reply shape the page reads, and the
X11/Qt fallback that queues the request to the Qt thread.

The room is a stand-in (the real one is riko.bubble_room.HyprlandBubbleRoom,
unit-tested against a fake hyprctl runner in test_bubble_room.py — the owner's
live session is never queried here either).
"""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
             "SWAYSOCK", "DBUS_SESSION_BUS_ADDRESS"):
    os.environ.pop(_var, None)

import desktop  # noqa: E402
import riko.bubble_room as br  # noqa: E402


class RouteRoom:
    """Stand-in for HyprlandBubbleRoom: records what the route asked for."""

    def __init__(self, granted=0):
        self.granted = granted
        self.calls = []
        self.abandoned = 0

    def apply(self, extra):
        self.calls.append(("apply", extra))
        self.granted = extra
        # the IO wrapper returns hyprctl argv as test evidence; the route
        # must strip it before the page sees the JSON
        return {"ok": True, "granted": extra, "reason": "grown",
                "geometry": (0, 0, 1, 1), "argv": ["hyprctl", "--batch", "…"]}

    def release(self):
        self.calls.append(("release",))
        self.granted = 0
        return {"ok": True, "granted": 0, "reason": "released",
                "geometry": (0, 0, 1, 1), "argv": ["hyprctl", "--batch", "…"]}

    def abandon(self):
        self.abandoned += 1
        self.granted = 0


def _post(room, extra, **kw):
    kw.setdefault("roam", False)
    kw.setdefault("visible", True)
    kw.setdefault("gesture", False)
    return desktop.handle_bubble_room_post(room, {"extra": extra}, **kw)


def test_route_grow_and_release_reach_the_room():
    room = RouteRoom()
    out = _post(room, 64)
    assert out["ok"] and out["granted"] == 64 and out["reason"] == "grown"
    assert "argv" not in out, "hyprctl argv is test evidence, not page payload"
    assert room.calls == [("apply", 64)]
    out = _post(room, 0)
    assert out == {"ok": True, "granted": 0, "reason": "released",
                   "geometry": (0, 0, 1, 1)}
    assert room.calls[-1] == ("release",)


def test_route_extra_is_rounded_and_junk_is_refused():
    room = RouteRoom()
    assert _post(room, "63.7")["granted"] == 64
    assert room.calls[-1] == ("apply", 64)
    assert _post(room, -10)["reason"] == "released"      # negative = release
    for junk in (None, "", "abc", {}, float("nan")):
        out = _post(RouteRoom(), junk)
        assert out["ok"] is False and "extra" in out["error"]


def test_route_roam_strip_never_grows_and_drops_the_bookkeeping():
    room = RouteRoom(granted=96)
    out = _post(room, 96, roam=True)
    assert out == {"ok": True, "granted": 0, "reason": "roam"}
    assert room.abandoned == 1
    assert room.calls == [], "the strip band must not be resized"


def test_route_defers_while_hidden_or_a_gesture_is_in_flight():
    room = RouteRoom(granted=96)
    out = _post(room, 128, visible=False)
    assert out["reason"] == "hidden" and out["granted"] == 96
    out = _post(room, 128, gesture=True)
    assert out["reason"] == "gesture" and out["granted"] == 96
    assert room.calls == [], "a deferred request never touches the window"


def test_route_without_window_control_answers_no_desktop():
    out = _post(None, 96)
    assert out == {"ok": True, "granted": 0, "reason": "no-desktop"}


def test_route_x11_path_queues_to_the_qt_thread():
    sent = []
    out = desktop.handle_bubble_room_post(None, {"extra": 80}, roam=False,
                                          visible=True, gesture=False,
                                          emit=sent.append)
    assert out == {"ok": True, "granted": 80, "reason": "queued"}
    assert sent == [80]
    out = desktop.handle_bubble_room_post(None, {"extra": 9e9}, roam=False,
                                          visible=True, gesture=False,
                                          emit=sent.append)
    assert out["granted"] == br.MAX_EXTRA          # the ceiling is in the reply
    # roam on the X11 path releases whatever the floating window had grown
    sent.clear()
    assert desktop.handle_bubble_room_post(None, {"extra": 64}, roam=True,
                                           visible=True, gesture=False,
                                           emit=sent.append)["reason"] == "roam"
    assert sent == [0]
    # deferred by a gesture in flight: nothing queued at all
    assert desktop.handle_bubble_room_post(None, {"extra": 64}, roam=False,
                                           visible=True, gesture=True,
                                           emit=sent.append)["reason"] == "gesture"
    assert sent == [0]
