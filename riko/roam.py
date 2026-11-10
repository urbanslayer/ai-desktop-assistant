"""Remember where she stopped on the roam strip (docs/WALK_MEMORY.md §W).

Tiny state file, ~/.config/riko/roam_x.json (or $RIKO_ROAM_STATE for tests).
The page POSTs /control/walk-state; desktop.py saves; GET /settings hands the
last x back so she resumes where she left off after a restart.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path


def state_path() -> Path:
    """Where the strip position lives; RIKO_ROAM_STATE overrides the whole file."""
    override = os.environ.get("RIKO_ROAM_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "riko" / "roam_x.json"


def strip_geometry(geo, base_height, scale, min_h=200, max_frac=0.6):
    """The strip rect (x, y, w, h) for an avatar scale (docs/WALK_MEMORY.md W).

    geo: the screen's available area (x, y, w, h). The strip is always full
    width and anchored to the BOTTOM edge; its height follows the scale
    (base window height x scale) — bigger her, taller the band she lives in.
    The height is clamped to [min_h, max_frac x screen height] so a tall
    monitor keeps a visible "band" feel and a tiny scale never cuts her off.
    """
    x, y, w, h = geo
    hh = max(min_h, min(int(int(base_height) * max(float(scale), 0.01)),
                        int(h * max_frac)))
    hh = min(hh, h)
    return (x, y + h - hh, w, hh)


def load_x() -> float | None:
    """The saved x in metres, or None (never raises)."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    x = data.get("x")
    if isinstance(x, (int, float)) and math.isfinite(x):
        return float(x)
    return None


def save_x(x) -> bool:
    """Persist x (metres). Bad values are ignored, not written."""
    if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(float(x)):
        return False
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"x": float(x)}), encoding="utf-8")
        return True
    except OSError:
        return False
