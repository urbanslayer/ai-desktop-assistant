"""Small runtime choices remembered across restarts, next to roam's x file.

Tiny state files in ~/.config/riko/ (or $RIKO_APP_STATE_DIR for tests).
Currently just the listening mode: desktop.listen_mode in
character_config.yaml seeds the initial default; the tray item, the L
shortcut or `riko-ctl listen-mode` override it at runtime and that choice
is what the next start honours.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("push_to_talk", "always")


def state_path(name: str) -> Path:
    """Where a named state file lives; RIKO_APP_STATE_DIR overrides the folder."""
    base = os.environ.get("RIKO_APP_STATE_DIR")
    if base:
        return Path(base).expanduser() / f"{name}.json"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "riko" / f"{name}.json"


def load_mode() -> str | None:
    """The persisted listening mode, or None (never raises)."""
    try:
        data = json.loads(state_path("listen_mode").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("mode") if isinstance(data, dict) else None
    return mode if mode in MODES else None


def save_mode(mode: str) -> bool:
    """Persist the listening mode. Bad values are ignored, not written."""
    if mode not in MODES:
        return False
    try:
        path = state_path("listen_mode")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": mode}), encoding="utf-8")
        return True
    except OSError:
        return False
