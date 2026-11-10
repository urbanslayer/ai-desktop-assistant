"""The desk side of touch (docs/TOUCH.md): press arbitration + sit-edge maths.

Pure Python — no Qt, no compositor, no clock. desktop.py owns the thin glue
that feeds real QEvents into PressArbiter and window/monitor numbers into
SitWatcher, and calls Qt's startSystemMove() when the arbiter says so. The
state machine here is unit-tested with synthetic event sequences.

Press arbitration (docs/TOUCH.md "Arbitration"):
    press on a limb rect  -> defer: the PAGE sees the press (we never consume
                             it); a move > MOVE_PX within HOLD_MS escalates
                             to a window drag (startSystemMove is legal while
                             the button is held), a still hold >= HOLD_MS
                             leaves the page holding a limb grab.
    press anywhere else   -> startSystemMove() at once (the old feel).
    Ctrl / roam strip / open input box / Shift keep their own rules — the
    glue only calls press() when none of them apply.
"""
from __future__ import annotations

import time

# ── arbitration timings (docs/TOUCH.md, exact) ──────────────────────
HOLD_MS = 250        # still this long over a limb -> the page grabs the limb
MOVE_PX = 6          # move further than this within HOLD_MS -> window drag

# limb rects the page sends in bbox.parts (docs/TOUCH.md); everything else
# on the window drags it, exactly like before touch existed.
LIMB_PARTS = frozenset({"head", "hand_left", "hand_right", "arm_left",
                        "arm_right", "foot_left", "foot_right"})

# ── sit-on-edge defaults (docs/TOUCH.md) ────────────────────────────
SIT_DELTA = 8        # hips within this many px of the screen edge -> sit
STAND_GAP = 40       # hips this far above the edge (while sitting) -> stand
REPUBLISH_PX = 4     # edge moved by at least this much -> re-emit sit:<y>
POLL_INTERVAL_S = 0.25   # 4 Hz while watching for a sit (never continuous)
POLL_WINDOW_S = 3.0      # ... and only for this long after a drag starts

# actions (verbs) the arbiter returns
MOVE = "move"        # caller: startSystemMove() + consume the press
DEFER = "defer"      # caller: pass the press through; watch for move vs hold
PASS = "pass"        # caller: pass through, arbiter stays idle


def now_ms() -> float:
    return time.monotonic() * 1000.0


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def limb_rects(bbox) -> list:
    """[(part, x, y, w, h)] from a bbox post's `parts` dict. A part may be
    [x,y,w,h] or {x,y,w,h}; junk entries are skipped, never raised."""
    out = []
    parts = (bbox or {}).get("parts")
    if not isinstance(parts, dict):
        return out
    for part, rect in parts.items():
        if str(part) not in LIMB_PARTS:
            continue
        if isinstance(rect, dict):
            raw = [rect.get(k) for k in ("x", "y", "w", "h")]
        elif isinstance(rect, (list, tuple)) and len(rect) == 4:
            raw = list(rect)
        else:
            continue
        vals = [v for v in raw if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(vals) != 4 or vals[2] <= 0 or vals[3] <= 0:
            continue
        out.append((str(part), float(vals[0]), float(vals[1]),
                    float(vals[2]), float(vals[3])))
    return out


def limb_at(bbox, x, y):
    """The limb part whose rect contains (x, y) (page CSS px), else None."""
    for part, rx, ry, rw, rh in limb_rects(bbox):
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return part
    return None


class PressArbiter:
    """Click/hold/drag arbitration for one window (docs/TOUCH.md).

    The caller drives it from the Qt event filter and acts on the returned
    verbs; it never touches Qt or a clock of its own (all times come in as
    the `now` argument, ms). States: idle -> pending (limb press, undecided)
    -> moving | grabbed, always back to idle on release.
    """

    IDLE, PENDING, MOVING, GRABBED = "idle", "pending", "moving", "grabbed"

    def __init__(self, hold_ms: float = HOLD_MS, move_px: float = MOVE_PX):
        self.hold_ms = float(hold_ms)
        self.move_px = float(move_px)
        self.state = self.IDLE
        self.part = None
        self._x = self._y = 0.0
        self._t0 = 0.0

    @property
    def pending(self) -> bool:
        return self.state == self.PENDING

    def press(self, x, y, now, *, inside_limb: bool) -> str:
        """A left press (Ctrl/Shift/strip/input-box already handled by the
        glue). MOVE = start the system move NOW and consume the event;
        DEFER = let the page have the press and watch for move vs hold;
        PASS = a stray press while pending (second button): page only."""
        if self.state == self.PENDING:
            return PASS          # already deciding: never hijack this press
        self._x, self._y, self._t0 = float(x), float(y), float(now)
        if inside_limb:
            self.state = self.PENDING
            return DEFER
        self.state = self.MOVING  # the old feel: instant window drag
        return MOVE

    def motion(self, x, y, now) -> str:
        """A pointer move while a press is in flight. MOVE = escalate to
        startSystemMove() (button still held — Qt allows it); GRAB = the
        hold crossed HOLD_MS, the page owns the limb now; NONE = keep
        deciding. Returns NONE in every non-pending state."""
        if self.state != self.PENDING:
            return "none"
        dt = float(now) - self._t0
        if dt >= self.hold_ms:
            self.state = self.GRABBED   # still (or slow) long enough: page's
            return "grab"
        if abs(float(x) - self._x) ** 2 + abs(float(y) - self._y) ** 2 > self.move_px ** 2:
            self.state = self.MOVING
            return MOVE
        return "none"

    def tick(self, now) -> str:
        """Timer callback (optional): decides a press with no motion after
        HOLD_MS. Same verdicts as motion()."""
        if self.state != self.PENDING:
            return "none"
        if float(now) - self._t0 >= self.hold_ms:
            self.state = self.GRABBED
            return "grab"
        return "none"

    def release(self, now) -> str:
        """The button came up. TAP = the page saw a whole press+release on a
        limb within the hold window (it POSTs /control/touch itself); NONE
        otherwise. Never consumes the release."""
        was = self.state
        self.state = self.IDLE
        return "tap" if was == self.PENDING else "none"


def edge_y_css(monitor_bottom: float, window_top: float, dpr: float = 1.0) -> float:
    """The sit line in page CSS px: how far the window's TOP sits above the
    monitor's bottom edge, divided by devicePixelRatio when the numbers came
    from a physical-px source (hyprctl). A floating window may hang below
    the screen — then this is small, which is exactly the sitting case."""
    dpr = float(dpr) or 1.0
    return (float(monitor_bottom) - float(window_top)) / dpr


class SitWatcher:
    """Sit-on-edge state machine over repeated (hips_y, edge_y) samples
    (docs/TOUCH.md). Emits 'sit:<edge_y>' / 'stand' command strings; re-emits
    sit when the edge moved >= republish px. hips_y None = the page has not
    posted one yet — no verdict, no state change."""

    def __init__(self, sit_delta: float = SIT_DELTA, stand_gap: float = STAND_GAP,
                 republish: float = REPUBLISH_PX):
        self.sit_delta = float(sit_delta)
        self.stand_gap = float(stand_gap)
        self.republish = float(republish)
        self.sitting = False
        self.edge_y = None      # edge of the LAST emitted sit command

    def update(self, hips_y, edge_y):
        """-> None | 'sit:<int>' | 'stand'."""
        hips = _num(hips_y)
        edge = _num(edge_y)
        if hips is None or edge is None:
            return None
        if not self.sitting:
            if hips >= edge - self.sit_delta:
                self.sitting = True
                self.edge_y = edge
                return f"sit:{round(edge)}"
            return None
        if hips < edge - self.stand_gap:
            self.sitting = False
            self.edge_y = None
            return "stand"
        if self.edge_y is None or abs(edge - self.edge_y) >= self.republish:
            self.edge_y = edge
            return f"sit:{round(edge)}"
        return None


# ── hyprctl helpers (fixture-tested; NEVER run against the live session) ──
def monitor_from_hypr(monitors: list, window_at) -> dict | None:
    """{bottom, scale, name} for the monitor a hyprctl client position
    (window_at=[x,y]) lives on, from `hyprctl -j monitors` rows. Physical
    px: bottom = position.y + size.h; `scale` is the monitor scale so the
    caller can divide to CSS px. Same pick order as avatar_scale's
    monitor_rect_for: containing-rect first, largest overlap second."""
    try:
        wx, wy = float(window_at[0]), float(window_at[1])
    except (TypeError, ValueError, IndexError):
        return None
    cands = []
    for m in monitors or []:
        if not isinstance(m, dict) or m.get("disabled"):
            continue
        # both hyprctl shapes: nested position/size lists and flat x/y/w/h
        pos, size = m.get("position"), m.get("size")
        try:
            if isinstance(pos, (list, tuple)) and isinstance(size, (list, tuple)):
                x, y = float(pos[0]), float(pos[1])
                w, h = float(size[0]), float(size[1])
            else:
                x, y = float(m["x"]), float(m["y"])
                w, h = float(m["width"]), float(m["height"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        try:
            scale = float(m.get("scale") or 1.0) or 1.0
        except (TypeError, ValueError):
            scale = 1.0
        cands.append((x, y, w, h, scale, str(m.get("name") or "")))
    if not cands:
        return None
    for x, y, w, h, scale, name in cands:
        if x <= wx < x + w and y <= wy < y + h:
            return {"bottom": y + h, "scale": scale, "name": name}
    best, best_area = None, 0.0
    for x, y, w, h, scale, name in cands:
        area = (max(0.0, min(wx, x + w) - max(wx, x)) *
                max(0.0, min(wy, y + h) - max(wy, y)))
        if area > best_area:
            best, best_area = {"bottom": y + h, "scale": scale, "name": name}, area
    return best
