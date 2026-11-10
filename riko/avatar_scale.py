"""Scroll-to-size math: plain scroll over her resizes the WINDOW, not the camera.

The page (client/index.html) owns the requested avatar scale (0.4x - 2.5x,
~8% wheel steps, smoothed toward the target) and POSTs it to
/control/avatar-scale (<= 20 Hz, final value when the wheel stops). This
module holds the pure desktop-side math so every part is testable without a
compositor or a window:

* clamp_scale / effective_scale  — the allowed band, plus the monitor-fit
  clamp (the window must fit its current monitor; past that the scale stops
  growing rather than cutting her off),
* scaled_geometry — the new window rect for a scale with her FEET anchored:
  the bottom-centre of the window stays at the same screen point, and the
  result is kept fully inside the monitor,
* hypr_scale_dispatches — the compositor dispatches for her OWN window only
  (Wayland clients cannot position themselves). Lua-config expressions
  (hl.dsp.window.resize / move, the owner's Hyprland 0.55+) and legacy
  hyprlang strings, same syntax conventions as desktop.py's startup rule;
  config-style detection is reused from riko/desktop_sense (files first,
  probe dispatch only when the files are ambiguous).
* apply_hyprland_scale — the IO wrapper: read own window row + monitors
  (hyprctl -j, read-only), compute, dispatch. `runner` replaces
  subprocess.run in tests (fake hyprctl rows), so it never needs a session.

Ctrl+scroll keeps the old camera dolly (a close-up crop) and double-click /
the tray's Reset size return scale 1.0 + the fit-to-body camera. Per
docs/DESKTOP_SENSE.md: reads are cheap -j calls, success of a dispatch is
the exact 'ok' reply, never the exit code.
"""
from __future__ import annotations

import json
import math

SCALE_MIN = 0.4
SCALE_MAX = 2.5
SCALE_STEP = 1.08           # ~8% per wheel notch
SCALE_DEFAULT = 1.0

# kept clear at the monitor edges so her window never touches a bar/edge
MONITOR_MARGIN = 20


def clamp_scale(value) -> float:
    """A finite scale inside [SCALE_MIN, SCALE_MAX]; junk -> SCALE_DEFAULT."""
    try:
        s = float(value)
    except (TypeError, ValueError):
        return SCALE_DEFAULT
    if not math.isfinite(s):
        return SCALE_DEFAULT
    return min(SCALE_MAX, max(SCALE_MIN, s))


def effective_scale(monitor, base_w: int, base_h: int, scale,
                    margin: int = MONITOR_MARGIN) -> float:
    """The scale actually applied: the request, capped so base x scale still
    fits inside the monitor (minus `margin`). Keeps the window's aspect, so
    the page's fit-to-body framing never crops her."""
    x, y, w, h = monitor
    s = clamp_scale(scale)
    if w > 2 * margin and h > 2 * margin and base_w > 0 and base_h > 0:
        s = min(s, (w - 2 * margin) / base_w, (h - 2 * margin) / base_h)
    return max(s, 0.01)  # a tiny monitor must not produce a zero/negative size


def scaled_geometry(monitor, current, base_w: int, base_h: int, scale,
                    margin: int = MONITOR_MARGIN):
    """(x, y, w, h) for `scale` with her feet anchored.

    monitor/current: (x, y, w, h) tuples — the monitor the window lives on
    and the window's CURRENT geometry. The bottom-centre of the window stays
    at the same screen point (she grows upward from her feet); the final
    rect is clamped so the whole window stays inside the monitor.
    """
    s = effective_scale(monitor, base_w, base_h, scale, margin)
    w = max(1, round(base_w * s))
    h = max(1, round(base_h * s))
    mx, my, mw, mh = monitor
    cx, cy, cw, ch = current
    bottom = cy + ch
    centre_x = cx + cw / 2.0
    x = round(centre_x - w / 2.0)
    y = bottom - h
    # fully on-screen (when the window is taller than the free space below
    # its old bottom, the TOP sticks out instead of her feet going under)
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
        scale = float(m.get("scale") or 1.0)
        x, y = int(m.get("x") or 0), int(m.get("y") or 0)
        transform = int(m.get("transform") or 0)
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


def apply_hyprland_scale(scale, base_w: int, base_h: int, app_id: str = "riko",
                         runner=None, config_dir=None, style=None,
                         margin: int = MONITOR_MARGIN):
    """Resize + place HER OWN window for `scale` through the compositor.

    Reads (hyprctl clients/monitors -j) are read-only ~ms calls; dispatches
    go through `hyprctl dispatch` — success is the exact 'ok' reply (hyprctl
    can exit 0 while printing an error). `runner` (tests) replaces
    subprocess.run wholesale; `style` skips the config-style detection
    (riko/desktop_sense: files first, probe only when ambiguous).

    Returns {ok, error?, scale, geometry?, dispatched?}. ok=True only when
    every dispatch answered 'ok'.
    """
    from .desktop_sense.util import run
    s = clamp_scale(scale)

    def reply(argv):
        done = run(argv, timeout=3, runner=runner)
        return done

    clients = _load_json(reply(["hyprctl", "clients", "-j"]).stdout)
    if not isinstance(clients, list):
        return {"ok": False, "error": "hyprctl clients failed (no JSON list "
                f"on stdout; got {str(clients)[:80]!r})", "scale": s}
    own = next((c for c in clients
                if isinstance(c, dict) and c.get("class") == app_id), None)
    if not own:
        classes = sorted({str(c.get("class")) for c in clients if isinstance(c, dict)})
        return {"ok": False, "error": f"own window not mapped yet ({len(clients)} "
                f"windows, classes {classes[:10]})", "scale": s}
    monitors = _load_json(reply(["hyprctl", "monitors", "-j"]).stdout)
    monitors = monitors if isinstance(monitors, list) else []
    monitor = monitor_rect_for(own, monitors)
    if not monitor:
        return {"ok": False, "error": f"monitor for own window unknown "
                f"({_monitor_diag(monitors, own)})", "scale": s}
    at = own.get("at") or [0, 0]
    size = own.get("size") or [base_w, base_h]
    current = (int(at[0]), int(at[1]), int(size[0]), int(size[1]))
    geometry = scaled_geometry(monitor, current, base_w, base_h, s, margin)
    if style is None:
        from .desktop_sense.hyprland import HyprlandBackend
        try:
            style = HyprlandBackend(runner=runner,
                                    config_dir=config_dir).config_style()
        except Exception:  # unreadable config: the Lua syntax is the owner's world
            style = "lua"
    address = own.get("address") or ""
    if style == "lua" and not address:
        return {"ok": False, "error": "own window address unknown", "scale": s}
    dispatched = []
    for expr in hypr_scale_dispatches(style, geometry[2], geometry[3],
                                      geometry[0], geometry[1], address, app_id):
        done = reply(["hyprctl", "dispatch", expr])
        out = (getattr(done, "stdout", "") or "").strip()
        if out != "ok":
            return {"ok": False, "error": out or "dispatch failed", "scale": s,
                    "geometry": geometry, "dispatched": dispatched + [expr]}
        dispatched.append(expr)
    return {"ok": True, "scale": s, "geometry": geometry, "dispatched": dispatched}
