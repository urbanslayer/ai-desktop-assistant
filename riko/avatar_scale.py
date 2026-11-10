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


def monitor_rect_for(clients_row: dict, monitors: list):
    """The (x, y, w, h) of the monitor a hyprctl client row lives on, or
    None. Rows carry `monitor` (id); -j monitors keys on `id`. Sizes come
    from `size` (0.5x) or the recommended mode; positions are absolute."""
    mid = clients_row.get("monitor")
    for m in monitors or []:
        if mid is not None and m.get("id") != mid:
            continue
        size = m.get("size")
        if isinstance(size, dict) and size.get("x") and size.get("y"):
            w, h = int(size["x"]), int(size["y"])
        else:
            mode = m.get("recommendedMode") or {}
            w, h = int(mode.get("width") or 0), int(mode.get("height") or 0)
        if w <= 0 or h <= 0:
            continue
        return (int(m.get("x", 0)), int(m.get("y", 0)), w, h)
    return None


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
        return {"ok": False, "error": "hyprctl clients failed", "scale": s}
    own = next((c for c in clients
                if isinstance(c, dict) and c.get("class") == app_id), None)
    if not own:
        return {"ok": False, "error": "own window not mapped yet", "scale": s}
    monitors = _load_json(reply(["hyprctl", "monitors", "-j"]).stdout)
    monitor = monitor_rect_for(own, monitors if isinstance(monitors, list) else [])
    if not monitor:
        return {"ok": False, "error": "monitor for own window unknown", "scale": s}
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
