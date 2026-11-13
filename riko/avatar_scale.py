"""Scroll-zoom window math: the page renders the zoom, this sizes the WINDOW.

Smoothness redesign (owner: the resize-driven zoom "jerks"): the page
(client/js/scale.js + client/index.html) glides a VISUAL scale and re-frames
her camera every frame, so she grows/shrinks in place continuously. The
window only moves at gesture boundaries — a HEADROOM grow at gesture start
and a FIT when the wheel goes quiet — and the page re-frames in the same
frame the canvas size changes, so the window moves are invisible. The page
POSTs /control/avatar-scale {scale, window, gesture}: `scale` is her final
size (persisted when the gesture ends), `window` the WINDOW scale the canvas
needs (headroom x final during a gesture, == final at the fit).

This module holds the pure desktop-side math + the compositor IO so every
part is testable without a compositor or a window:

* clamp_scale / clamp_window_scale / effective_scale — the allowed band
  (window scales may exceed it for gesture headroom), plus the monitor-fit
  clamp (the window must fit its current monitor; past that the size stops
  growing rather than cutting her off),
* scaled_geometry — the new window rect for a scale with her FEET anchored:
  the bottom-centre of the window stays at the same screen point, and the
  result is kept fully inside the monitor,
* hypr_scale_dispatches / hypr_scale_batch — the compositor commands for HER
  OWN window only (Wayland clients cannot position themselves). Lua-config
  expressions (hl.dsp.window.resize / move, the owner's Hyprland 0.55+) and
  legacy hyprlang strings; config-style detection is reused from
  riko/desktop_sense (files first, probe dispatch only when ambiguous).
  Resize + move travel as ONE `hyprctl --batch` request (verified against
  the v0.56.2 source: hyprctl/src/main.cpp batchRequest sends the rest of
  the command line as a single `[[BATCH]]` IPC request; src/debug/HyprCtl.cpp
  dispatchBatch splits it on ';' outside brackets and runs each part through
  the normal dispatch path, replies joined by three newlines),
* HyprlandSizer — the IO wrapper with caches: her window address + geometry
  and the monitor list are read once and kept (monitors refresh every
  MONITOR_CACHE_TTL s; everything refreshes after a failure, and the last
  dispatched geometry is authoritative between applies because only we move
  the window). Every apply is ONE batched subprocess call. `runner` replaces
  subprocess.run in tests (fake hyprctl rows), so it never needs a session.

Ctrl+scroll keeps the old camera dolly (a close-up crop) and double-click /
the tray's Reset size return scale 1.0 + the fit-to-body camera. Per
docs/DESKTOP_SENSE.md: reads are cheap -j calls, success of a dispatch is
the exact 'ok' reply, never the exit code.
"""
from __future__ import annotations

import json
import math
import threading
import time

SCALE_MIN = 0.4
SCALE_MAX = 2.5
SCALE_STEP = 1.08           # ~8% per wheel notch
SCALE_DEFAULT = 1.0

# Gesture headroom can push the WINDOW above the visual size band (a 2.5x
# zoom asks for a 4x canvas before the monitor clamp bounds it).
WINDOW_SCALE_MAX = SCALE_MAX * 2.0

# kept clear at the monitor edges so her window never touches a bar/edge
MONITOR_MARGIN = 20

# monitor list reuse window (s); a failed apply refreshes everything anyway
MONITOR_CACHE_TTL = 30.0

# hyprctl batch replies join per-command outputs with exactly this
BATCH_SEP = "\n\n\n"


def _finite_scale(value, default: float):
    try:
        s = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(s):
        return default
    return s


def clamp_scale(value) -> float:
    """A finite scale inside [SCALE_MIN, SCALE_MAX]; junk -> SCALE_DEFAULT."""
    return min(SCALE_MAX, max(SCALE_MIN, _finite_scale(value, SCALE_DEFAULT)))


def clamp_window_scale(value) -> float:
    """A WINDOW scale: the band's lower edge plus WINDOW_SCALE_MAX ceiling
    (gesture headroom above SCALE_MAX is legal — the monitor clamp bounds
    it for real); junk -> SCALE_DEFAULT."""
    return min(WINDOW_SCALE_MAX, max(SCALE_MIN, _finite_scale(value, SCALE_DEFAULT)))


def effective_scale(monitor, base_w: int, base_h: int, scale,
                    margin: int = MONITOR_MARGIN,
                    max_scale: float = SCALE_MAX, avail_h=None) -> float:
    """The scale actually applied: the request, capped so base x scale still
    fits inside the monitor (minus `margin`), AND — when `avail_h` is given
    — inside the space actually available ABOVE the anchored feet line (the
    window grows upward from the feet, so a window whose feet sit mid-screen
    can only grow up to the monitor's usable top edge). Keeps the window's
    aspect, so the page's fit-to-body framing never crops her. `max_scale`
    lifts the ceiling for gesture headroom windows (clamp_window_scale
    already bounds the request itself)."""
    x, y, w, h = monitor
    s = min(_finite_scale(scale, SCALE_DEFAULT), max_scale)
    s = max(s, 0.01)
    if w > 2 * margin and h > 2 * margin and base_w > 0 and base_h > 0:
        s = min(s, (w - 2 * margin) / base_w, (h - 2 * margin) / base_h)
        if avail_h is not None:
            s = min(s, max(float(avail_h), 1.0) / base_h)
    return max(s, 0.01)  # a tiny monitor must not produce a zero/negative size


def scaled_geometry(monitor, current, base_w: int, base_h: int, scale,
                    margin: int = MONITOR_MARGIN,
                    max_scale: float = SCALE_MAX):
    """(x, y, w, h) for `scale` with her feet anchored.

    monitor/current: (x, y, w, h) tuples — the monitor the window lives on
    and the window's CURRENT geometry. The bottom-centre of the window stays
    at the same screen point (she grows upward from her feet). Growth that
    would push the top edge past the monitor's usable top is clamped AWAY
    (the size stops) rather than clamping y — clamping y would drag the
    bottom edge down and move her feet, which is exactly the jump this
    design forbids. Horizontal clamp may slide her along the monitor; x is
    kept fully on-screen.
    """
    mx, my, mw, mh = monitor
    cx, cy, cw, ch = current
    bottom = cy + ch
    centre_x = cx + cw / 2.0
    s = effective_scale(monitor, base_w, base_h, scale, margin, max_scale,
                        avail_h=bottom - my)
    w = max(1, round(base_w * s))
    h = max(1, round(base_h * s))
    x = round(centre_x - w / 2.0)
    y = bottom - h
    # fully on-screen (the avail clamp keeps y >= my; this also handles a
    # window that started partially off the top)
    x = max(mx, min(x, mx + mw - w))
    y = max(my, min(y, my + mh - h))
    return (int(x), int(y), int(w), int(h))


def hypr_scale_dispatches(style: str, w: int, h: int, x: int, y: int,
                          address: str, app_id: str = "riko") -> list[str]:
    """Dispatch expressions (one argv element each) sizing AND placing HER
    window. Both styles select only her own window: Lua by address, legacy
    by the app-id class — never another window. Resize first, then place:
    feet anchoring assumes the final size.

    Lua (Hyprland 0.55+, the owner's config): the same hl.dsp.window.* calls
    desktop.py's startup fixup uses (x/y absolute = exact pixels, relative
    defaults false). Legacy hyprlang keeps the quoted classic strings
    resizewindowpixel/movewindowpixel exact <x> <y>,class:^(id)$.
    """
    if style == "lua":
        wref = f'window="address:{address}"'
        return [
            f'hl.dsp.window.resize({{x={w}, y={h}, {wref}}})',
            f'hl.dsp.window.move({{x={x}, y={y}, {wref}}})',
        ]
    sel = f"class:^({app_id})$"
    return [
        f'"resizewindowpixel exact {w} {h},{sel}"',
        f'"movewindowpixel exact {x} {y},{sel}"',
    ]


def hypr_scale_batch(style: str, w: int, h: int, x: int, y: int,
                     address: str, app_id: str = "riko") -> list[str]:
    """ONE hyprctl argv that resizes AND moves her window in one compositor
    round trip: `hyprctl --batch 'dispatch <resize> ; dispatch <move>'`
    (v0.56.2 source-verified: one IPC request, split server-side on ';'
    outside brackets, each part through the normal dispatch path). The Lua
    expressions contain no ';' and no '[]', so the split can not cut them.
    """
    exprs = hypr_scale_dispatches(style, w, h, x, y, address, app_id)
    return ["hyprctl", "--batch", " ; ".join(f"dispatch {e}" for e in exprs)]


def batch_reply_ok(text):
    """(ok, error) for a hyprctl --batch reply of TWO dispatches: both
    parts must say 'ok' (hyprctl exits 0 even when a part errors) and a
    short reply means a partial apply; `error` carries what went wrong."""
    parts = [p.strip() for p in (text or "").split(BATCH_SEP)]
    if len(parts) != 2:
        return False, ((text or "").strip()[:120] or "empty reply")
    bad = next((p for p in parts if p != "ok"), "")
    return (not bad), bad


def monitor_logical_rect(m: dict):
    """Usable LOGICAL (x, y, w, h) of one real `hyprctl -j monitors` row, or
    None when the row is disabled/unusable (keys verified against live
    Hyprland 0.56.2, tests/fixtures/hypr_real_monitors.json).

    Real rows carry PHYSICAL mode pixels in `width`/`height` plus a `scale`
    (e.g. 1.25); `x`/`y` are already logical layout coords (owner's layout:
    HDMI-A-1 at x=5120 = 2560+2560 of its two scale-1 neighbours, logical
    width 2560/1.25=2048). `transform` 1/3/5/7 rotate 90/270 -> swap w/h.
    `reserved` [l, t, r, b] are bar reservations — subtract them so the
    usable area excludes the bars. Window coords/dispatches share this
    logical space (client row size [420, 680] == requested base size on the
    1.25-scale monitor)."""
    if not isinstance(m, dict) or m.get("disabled"):
        return None
    try:
        w = int(m.get("width") or 0)
        h = int(m.get("height") or 0)
        scale = float(m.get("scale", 1.0))   # NOT `or 1.0`: an explicit
        x, y = int(m.get("x") or 0), int(m.get("y") or 0)  # scale 0 is junk,
        transform = int(m.get("transform") or 0)           # not "missing"
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or scale <= 0:
        return None
    if transform in (1, 3, 5, 7):
        w, h = h, w
    lw, lh = round(w / scale), round(h / scale)
    res = m.get("reserved")
    if isinstance(res, (list, tuple)) and len(res) == 4:
        try:
            left, top, right, bottom = (int(v) for v in res)
        except (TypeError, ValueError):
            left = top = right = bottom = 0
        x += left
        y += top
        lw -= left + right
        lh -= top + bottom
    if lw <= 0 or lh <= 0:
        return None
    return (x, y, lw, lh)


def monitor_rect_for(clients_row: dict, monitors: list):
    """The usable logical (x, y, w, h) of the monitor a hyprctl client row
    lives on, or None. The row's `monitor` field is the monitor `id`; when
    it is missing/-1 (unassigned) fall back to the monitor whose logical
    rect contains the window centre, else the one with the largest overlap.
    Disabled monitors never match."""
    if not isinstance(clients_row, dict):
        return None
    mid = clients_row.get("monitor")
    mid = mid if isinstance(mid, int) and not isinstance(mid, bool) else None
    rects = []
    for m in monitors or []:
        rect = monitor_logical_rect(m)
        if rect is None:
            continue
        if mid is not None and mid >= 0 and isinstance(m, dict) and m.get("id") == mid:
            return rect
        rects.append(rect)
    if not rects:
        return None
    try:
        at, size = clients_row.get("at") or [0, 0], clients_row.get("size") or [0, 0]
        wx, wy, ww, wh = int(at[0]), int(at[1]), int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    cx, cy = wx + ww / 2.0, wy + wh / 2.0
    for rx, ry, rw, rh in rects:
        if rx <= cx < rx + rw and ry <= cy < ry + rh:
            return (rx, ry, rw, rh)
    # centre on no monitor (straddling/offscreen): largest overlap wins
    best, best_area = None, 0
    for rx, ry, rw, rh in rects:
        area = (max(0, min(wx + ww, rx + rw) - max(wx, rx))
                * max(0, min(wy + wh, ry + rh) - max(wy, ry)))
        if area > best_area:
            best, best_area = (rx, ry, rw, rh), area
    return best


def _monitor_diag(monitors, clients_row) -> str:
    """Numbers for a '[size]' failure line: window's monitor id + centre,
    the monitor ids seen and the KEYS the rows carry, so a future Hyprland
    schema mismatch is diagnosable from the log alone."""
    ids = [m.get("id") for m in monitors or [] if isinstance(m, dict)]
    keys = sorted({k for m in monitors or [] if isinstance(m, dict) for k in m})
    return (f"window monitor id {clients_row.get('monitor')!r}, "
            f"at/size {clients_row.get('at')}/{clients_row.get('size')}, "
            f"monitor ids {ids}, keys present {keys}")


def _load_json(text):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return None


class HyprlandSizer:
    """Sizes HER OWN window under Hyprland with compositor-round-trip
    discipline: each apply is ONE `hyprctl --batch` dispatch (resize+move in
    one IPC request). State caches — her window address, her geometry, the
    monitor rect — are read once (`hyprctl -j clients/monitors`, read-only
    ~ms) and kept: the page only asks for window moves at gesture
    boundaries, and between them the last dispatched geometry is
    authoritative because only we move the window. Monitors refresh after
    MONITOR_CACHE_TTL seconds (mode/scale changes) or after any failure;
    the window row refreshes after a failure too (an unmapped or recreated
    window changes the address). Feet anchoring across a grow-then-fit
    gesture falls out of the cached geometry: each apply anchors on the
    rect the previous apply established.
    """

    def __init__(self, base_w: int, base_h: int, app_id: str = "riko",
                 runner=None, config_dir=None, style=None,
                 margin: int = MONITOR_MARGIN, clock=time.monotonic):
        self.base_w, self.base_h = base_w, base_h
        self.app_id = app_id
        self.runner = runner
        self.config_dir = config_dir
        self.style = style          # None -> resolve lazily (files first)
        self.margin = margin
        self._clock = clock
        self._monitor = None        # usable logical rect of her monitor
        self._address = ""          # her window address (Lua selector)
        self._geom = None           # last known/dispatched (x, y, w, h)
        self._monitors_at = 0.0
        self._lock = threading.Lock()

    # ── cached reads ──────────────────────────────────────────────────
    def invalidate(self):
        self._monitor = None
        self._geom = None
        self._monitors_at = 0.0

    def _read_state(self):
        """(error, own_row): refresh monitors + her window row/geometry."""
        from .desktop_sense.util import run
        clients = _load_json(run(["hyprctl", "clients", "-j"], timeout=3,
                                 runner=self.runner).stdout)
        if not isinstance(clients, list):
            return (f"hyprctl clients failed (no JSON list on stdout; got "
                    f"{str(clients)[:80]!r})"), None
        own = next((c for c in clients if isinstance(c, dict)
                    and c.get("class") == self.app_id), None)
        if not own:
            classes = sorted({str(c.get("class")) for c in clients if isinstance(c, dict)})
            return (f"own window not mapped yet ({len(clients)} windows, "
                    f"classes {classes[:10]})"), None
        monitors = _load_json(run(["hyprctl", "monitors", "-j"], timeout=3,
                                  runner=self.runner).stdout)
        monitors = monitors if isinstance(monitors, list) else []
        rect = monitor_rect_for(own, monitors)
        if not rect:
            return f"monitor for own window unknown ({_monitor_diag(monitors, own)})", None
        try:
            at = own.get("at") or [0, 0]
            size = own.get("size") or [self.base_w, self.base_h]
            geom = (int(at[0]), int(at[1]), int(size[0]), int(size[1]))
        except (TypeError, ValueError, IndexError):
            return (f"own window geometry unreadable "
                    f"(at/size {own.get('at')}/{own.get('size')})"), None
        self._monitor = rect
        self._address = own.get("address") or ""
        self._geom = geom
        self._monitors_at = self._clock()
        return None, own

    def _ensure_state(self, force=False):
        if force or self._monitor is None or self._geom is None:
            return self._read_state()
        if self._clock() - self._monitors_at > MONITOR_CACHE_TTL:
            # monitors may have changed (mode/scale/plug): re-read everything
            return self._read_state()
        return None, None

    def _resolve_style(self):
        if self.style is None:
            from .desktop_sense.hyprland import HyprlandBackend
            try:
                self.style = HyprlandBackend(runner=self.runner,
                                             config_dir=self.config_dir).config_style()
            except Exception:  # unreadable config: the Lua syntax is the owner's world
                self.style = "lua"
        return self.style

    # ── shared compositor state (riko/bubble_room.py grows on top of it) ──
    def window_state(self, force: bool = False):
        """(error, {monitor, geometry, address}) — her cached window + monitor
        state, exposed for the other window mover (bubble headroom) so one
        reader owns the `hyprctl -j` round trips. Refreshes when nothing is
        cached, when `force`, or when the monitor cache aged out."""
        with self._lock:
            err, _ = self._ensure_state(force=force)
            if err:
                return err, None
            return None, {"monitor": self._monitor, "geometry": self._geom,
                          "address": self._address}

    def dispatch_geometry(self, geometry):
        """Put her window at an EXACT logical (x, y, w, h) — no scale math —
        in the same ONE batched dispatch (resize + move) `apply()` uses.
        {ok, geometry, argv, error}: the cached geometry becomes this rect on
        success (we are the only mover) and everything refreshes on failure."""
        x, y, w, h = (int(v) for v in geometry)
        with self._lock:
            err, _ = self._ensure_state()
            if err:
                return {"ok": False, "error": err}
            style = self._resolve_style()
            if style == "lua" and not self._address:
                self.invalidate()
                return {"ok": False, "error": "own window address unknown"}
            argv = hypr_scale_batch(style, w, h, x, y, self._address, self.app_id)
            from .desktop_sense.util import run
            done = run(argv, timeout=3, runner=self.runner)
            ok, error = batch_reply_ok(getattr(done, "stdout", ""))
            if not ok:
                self.invalidate()
                return {"ok": False, "error": error or "dispatch failed",
                        "geometry": (x, y, w, h), "argv": argv}
            self._geom = (x, y, w, h)
            return {"ok": True, "geometry": (x, y, w, h), "argv": argv}

    # ── the applies ───────────────────────────────────────────────────
    def fit_cap(self):
        """The largest FINAL scale her monitor can show (the page clamps
        wheel targets to it so 'stop growing' is visible, not a cropped
        head). One cheap -j pair at worst; cached after."""
        with self._lock:
            return self._fit_cap_locked()[0]

    def _fit_cap_locked(self):
        err, _ = self._ensure_state()
        if err or self._monitor is None or self._geom is None:
            return SCALE_MAX, err
        # growth room = the usable monitor height ABOVE her current feet
        # line (bottom edge), aspect kept — the honest "stop growing" point
        avail_h = self._geom[1] + self._geom[3] - self._monitor[1]
        return (effective_scale(self._monitor, self.base_w, self.base_h,
                                SCALE_MAX, self.margin, avail_h=avail_h), None)

    def apply(self, window_scale):
        """Put HER window at `window_scale` (feet anchored, monitor-clamped,
        one batched dispatch). Returns {ok, scale, max, geometry?, argv?,
        error?}: `scale` is the window scale actually applied and `max` the
        monitor-fit ceiling for final scales — the page keeps its headroom
        and its clamps honest from these numbers alone."""
        s = clamp_window_scale(window_scale)
        with self._lock:
            err, _ = self._ensure_state()
            if err:
                return {"ok": False, "error": err, "scale": s}
            max_scale, err = self._fit_cap_locked()
            if err:
                return {"ok": False, "error": err, "scale": s}
            geometry = scaled_geometry(self._monitor, self._geom,
                                       self.base_w, self.base_h, s, self.margin,
                                       max_scale=WINDOW_SCALE_MAX)
            style = self._resolve_style()
            if style == "lua" and not self._address:
                self.invalidate()
                return {"ok": False, "error": "own window address unknown",
                        "scale": s, "max": max_scale}
            argv = hypr_scale_batch(style, geometry[2], geometry[3],
                                    geometry[0], geometry[1], self._address,
                                    self.app_id)
            from .desktop_sense.util import run
            done = run(argv, timeout=3, runner=self.runner)
            ok, error = batch_reply_ok(getattr(done, "stdout", ""))
            if not ok:
                # anything can have changed (window closed/recreated,
                # monitor layout, focus rules): full refresh before the next
                self.invalidate()
                return {"ok": False, "error": error or "dispatch failed",
                        "scale": s, "max": max_scale, "geometry": geometry,
                        "argv": argv}
            self._geom = geometry   # we are the only mover: authoritative
            return {"ok": True, "scale": min(s, max_scale), "max": max_scale,
                    "geometry": geometry, "argv": argv}
