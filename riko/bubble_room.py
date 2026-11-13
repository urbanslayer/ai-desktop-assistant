"""Bubble headroom: her window grows UPWARD while she talks, feet stay put.

Why (docs/TOUCH.md "Speech bubble headroom"): at the default 420x680 window
there are only 8-28 px above her head, so `placeBubble`'s preferred `above`
mode never fits and the speech bubble always falls back onto her chest. The
owner's decision: while she is talking, make the room by growing the window
upward (feet and her size unchanged) and give it back afterwards.

The seam (page → desktop, `POST /control/bubble-room`):
    {"extra": <px>}   extra > 0: keep this MANY px of room above my head
                      extra == 0: the bubble is gone, give the window back
    reply: {"ok": bool, "granted": <px>, "reason": str, "geometry"?, "argv"?}
`granted` is what the desktop could really give (the monitor's usable top
clamps it); the page keeps the chest placement while granted < requested or
the request is deferred, so her face is never covered.

Grow geometry is exactly `y -= extra, h += extra` with x and w untouched,
computed from the CURRENT rect against her feet line (the window's bottom
edge), so:
  * a re-request with the same number is a no-op (no compositor churn),
  * a user drag while grown is respected (release keeps the feet where the
    user put them),
  * an external resize (a scale FIT landing mid-bubble) is repaired by the
    page's keep-alive re-request.

Nothing here persists anything: her saved window size/position
(appstate avatar_scale.json) stays the pre-grow one, and the grown geometry
is only ever the transient state of this module.

Everything in this file is pure (no compositor, no Qt):
  * room_needed / round_step — the arithmetic the page runs too, mirrored
    1:1 in client/js/bubble_room.js so both sides agree,
  * grow_target / release_target — the exact rects,
  * BubbleRoom — grow / keep / release state machine (the desktop guards —
    roam strip, hidden window, scale gesture in flight, window drag, sitting —
    are inputs from desktop.py, not decisions here),
  * HyprlandBubbleRoom — the IO wrapper: it borrows the scroll-zoom
    HyprlandSizer's cached window + monitor state and its ONE batched hyprctl
    dispatch (riko/avatar_scale.py) to place an exact rect. Tests inject a
    fake runner, so no session is ever touched.
"""
from __future__ import annotations

import math

# request granularity (px): a few grows per bubble at most, never one per
# frame of sway
STEP = 16
# hard ceiling: a 6+-sentence reply at the default window wants ~200 px
MAX_EXTRA = 220
# must match bubblePlacement's `gap` / `margin` (client/index.html)
GAP = 8
MARGIN = 8
# the page waits this long after the bubble hides before releasing, so
# consecutive lines/replies never pump the window up and down
RELEASE_DELAY_MS = 1500


def round_step(px, step: int = STEP) -> int:
    """Round a px need UP to the next `step` (0 stays 0)."""
    n = float(px)
    if not math.isfinite(n) or n <= 0:
        return 0
    return int(math.ceil(n / float(step)) * step)


def room_needed(head_top_y, bubble_h, gap: int = GAP, margin: int = MARGIN,
                step: int = STEP, max_extra: int = MAX_EXTRA) -> int:
    """Extra px of window above her head so a bubble of `bubble_h` fits in
    the `above` slot: that slot is `head_top_y - gap - margin` tall today, so
    the shortfall is `bubble_h + gap + margin - head_top_y`, rounded up to
    `step` and capped at `max_extra`. 0 when it already fits (or junk)."""
    try:
        head = float(head_top_y)
        h = float(bubble_h)
    except (TypeError, ValueError):
        return 0
    if not (math.isfinite(head) and math.isfinite(h)):
        return 0
    need = h + gap + margin - head
    if need <= 0:
        return 0
    return int(min(float(max_extra), float(round_step(need, step))))


def _rect(value):
    x, y, w, h = value
    return int(x), int(y), int(w), int(h)


def grow_target(current, base_w: int, base_h: int, extra, monitor=None,
                max_extra: int = MAX_EXTRA):
    """(granted, (x, y, w, h)) for `extra` px of headroom.

    current: her CURRENT window rect; base_w/base_h: the window's NORMAL
    (pre-grow) size; monitor: usable logical rect to clamp against.
    The window's bottom edge (her feet line) is the anchor: x and w are kept,
    h becomes base_h + granted and y becomes bottom - h, i.e. `y -= extra,
    h += extra` while nothing moves below her feet. Growth past the monitor's
    usable top (reserved bar included) is granted PARTIALLY — exactly what
    avatar_scale.scaled_geometry does for zoom (feet anchoring beats filling
    the screen) — so the reply's `granted` is the number the page can trust.
    """
    cx, cy, cw, ch = _rect(current)
    bottom = cy + ch
    try:
        want = int(round(float(extra)))
    except (TypeError, ValueError):
        want = 0
    want = max(0, min(want, int(max_extra)))
    w = int(base_w)
    total = int(base_h) + want
    x = cx
    if monitor is not None:
        mx, my, mw, mh = _rect(monitor)
        avail = int(bottom - my)              # room above the feet line
        total = max(int(base_h), min(total, avail))
        x = max(mx, min(x, mx + mw - w))      # keep her on screen
    granted = max(0, total - int(base_h))
    h = int(base_h) + granted
    return granted, (int(x), int(bottom - h), int(w), h)


def release_target(current, base_w: int, base_h: int, monitor=None):
    """The rect that gives the room back: the NORMAL size, anchored on the
    CURRENT bottom edge (wherever the user dragged her feet to) and x.
    No drag while grown → this is the pre-grow rect EXACTLY."""
    cx, cy, cw, ch = _rect(current)
    bottom = cy + ch
    w, h = int(base_w), int(base_h)
    x = cx
    if monitor is not None:
        mx, my, mw, mh = _rect(monitor)
        x = max(mx, min(x, mx + mw - w))
    return (int(x), int(bottom - h), w, h)


class BubbleRoom:
    """Grow / keep / release state machine for one window (pure).

    The desktop guards (roam strip, hidden, gesture in flight, drag, sitting)
    are checked by the caller BEFORE calling in; this class only remembers
    what it grew and what it dispatched, and answers three questions:
    `want(extra, current, monitor)`, `released(current, monitor)`, and the
    post-dispatch bookkeeping `applied()` / `failed()` / `reset()`.
    """

    def __init__(self, max_extra: int = MAX_EXTRA):
        self.max_extra = int(max_extra)
        self.base = None        # normal (w, h), remembered at the first grow
        self.saved = None       # full pre-grow rect (evidence / restore exactness)
        self.granted = 0        # px of room currently granted
        self.geometry = None    # last successfully dispatched rect

    @property
    def grown(self) -> bool:
        return self.granted > 0

    def reset(self):
        """Forget everything WITHOUT dispatching (the roam strip took the
        window over, the window was recreated, the app is shutting down)."""
        self.base = None
        self.saved = None
        self.granted = 0
        self.geometry = None

    def want(self, extra, current, monitor=None) -> dict:
        """Ask for `extra` px of room. Returns
        {action: 'grow'|'already'|'no-room', granted, geometry, reason}.
        'grow' means the caller must dispatch `geometry`; 'already' means the
        window is where we want it (the page's keep-alive re-ask lands here,
        and so does a drag while grown)."""
        try:
            want = int(round(float(extra)))
        except (TypeError, ValueError):
            want = 0
        if want <= 0:
            return self.released(current, monitor)
        cur = _rect(current)
        base = self.base or (cur[2], cur[3])
        if self.grown:
            # never shrink while she is talking: a shorter line does not take
            # the room back mid-speech (only the release after it does)
            want = max(want, self.granted)
        granted, geom = grow_target(cur, base[0], base[1], want, monitor,
                                    self.max_extra)
        if granted <= 0:
            # at the top of the screen with nothing above the head: keep the
            # window exactly as it is, the page stays on the chest placement
            return {"action": "no-room", "granted": 0, "geometry": None,
                    "reason": "no-room"}
        if not self.grown:
            self.base = (base[0], base[1])
            self.saved = cur
        self.granted = granted
        # 'already' covers both the page's keep-alive (target == what we
        # dispatched) and a user drag (target == where she is NOW): in both
        # cases the compositor must not be touched at all. After a failed
        # dispatch (`geometry is None`) nothing is trusted: re-dispatch.
        changed = self.geometry != geom and (geom != cur or self.geometry is None)
        return {"action": "grow" if changed else "already",
                "granted": granted, "geometry": geom,
                "reason": "grown" if changed else "already"}

    def released(self, current, monitor=None) -> dict:
        """Give the room back. 'idle' = nothing was grown (the page's release
        on a bubble that never needed room lands here — no dispatch)."""
        if not self.grown or self.base is None:
            self.granted = 0
            return {"action": "idle", "granted": 0, "geometry": None,
                    "reason": "idle"}
        geom = release_target(_rect(current), self.base[0], self.base[1], monitor)
        return {"action": "release", "granted": 0, "geometry": geom,
                "reason": "released"}

    def applied(self, geometry):
        """Book a successful dispatch (this rect is now the truth)."""
        self.geometry = _rect(geometry)

    def failed(self):
        """A dispatch failed: the window is NOT what we thought, so the next
        request must re-dispatch from whatever the compositor really has."""
        self.geometry = None


class HyprlandBubbleRoom:
    """IO wrapper: put her window at the grown / restored rect under Hyprland.

    Reuses the scroll-zoom sizer (riko/avatar_scale.HyprlandSizer) for
    everything: her window address, her geometry, the monitor rect (cached,
    refreshed after any failure) and the ONE batched `hyprctl --batch`
    dispatch that resizes AND moves her own window. `sizer=None` (no
    Hyprland) makes every call a clean no-op so the route always answers.
    """

    def __init__(self, sizer, max_extra: int = MAX_EXTRA):
        self.sizer = sizer
        self.state = BubbleRoom(max_extra=max_extra)
        self._dirty = False    # a user drag happened: re-read before deciding

    def mark_dirty(self):
        """The user moved her window (desktop.py's drag end): the cached
        geometry is stale, so the next decision reads the compositor again —
        release must restore around WHERE SHE IS NOW, not where she was."""
        self._dirty = True

    @property
    def granted(self) -> int:
        return self.state.granted

    def _decision(self, kind, extra, current, monitor):
        if kind == "want":
            return self.state.want(extra, current, monitor)
        return self.state.released(current, monitor)

    def _run(self, kind, extra=0):
        if self.sizer is None:
            return {"ok": True, "granted": self.state.granted if kind == "want" else 0,
                    "reason": "no-desktop"}
        if kind == "release" and not self.state.grown:
            return {"ok": True, "granted": 0, "reason": "idle", "geometry": None}
        # a release is the one moment the true geometry matters (the user may
        # have dragged her); a keep-alive may trust the cache and stay quiet
        err, st = self.sizer.window_state(force=self._dirty or kind == "release")
        self._dirty = False
        if err:
            return {"ok": False, "error": err, "granted": self.state.granted,
                    "reason": "no-window"}
        d = self._decision(kind, extra, st["geometry"], st["monitor"])
        if d["action"] in ("already", "no-room", "idle"):
            return {"ok": True, "granted": d["granted"], "reason": d["reason"],
                    "geometry": d["geometry"]}
        res = self.sizer.dispatch_geometry(d["geometry"])
        if not res.get("ok"):
            self.state.failed()
            return {"ok": False, "error": res.get("error", "dispatch failed"),
                    "granted": self.state.granted, "reason": "dispatch-failed",
                    "argv": res.get("argv")}
        self.state.applied(res["geometry"])
        return {"ok": True, "granted": self.state.granted, "reason": d["reason"],
                "geometry": res["geometry"], "argv": res["argv"]}

    def apply(self, extra) -> dict:
        return self._run("want", extra)

    def release(self) -> dict:
        out = self._run("release")
        if out.get("reason") in ("released", "idle"):
            self.state.reset()
            out["granted"] = 0
        return out

    def abandon(self):
        """The window geometry is no longer ours (roam strip takeover): forget
        the grown state without touching the window."""
        self.state.reset()
