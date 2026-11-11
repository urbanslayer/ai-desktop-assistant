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


def load_scale(persona_id) -> float | None:
    """The avatar window scale saved for one persona, or None (never raises).

    avatar_scale.json holds {persona_id: scale} — each persona opens at the
    size the owner last scrolled her to. Values outside riko.avatar_scale's
    band are ignored as if never saved.
    """
    key = str(persona_id or "default")
    try:
        data = json.loads(state_path("avatar_scale").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    from . import avatar_scale
    value = data.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if avatar_scale.SCALE_MIN <= float(value) <= avatar_scale.SCALE_MAX:
            return float(value)
    return None


def save_scale(persona_id, scale) -> bool:
    """Persist one persona's scale. Bad values are ignored, not written."""
    from . import avatar_scale
    if isinstance(scale, bool):
        return False
    try:
        s = float(scale)
    except (TypeError, ValueError):
        return False
    if not avatar_scale.SCALE_MIN <= s <= avatar_scale.SCALE_MAX:
        return False
    key = str(persona_id or "default")
    try:
        path = state_path("avatar_scale")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass
        data[key] = s
        path.write_text(json.dumps(data), encoding="utf-8")
        return True
    except OSError:
        return False


def load_name_gate() -> bool | None:
    """The persisted always-listen name gate (riko/name_gate.py), or None
    when never chosen (never raises)."""
    try:
        data = json.loads(state_path("listen_name_gate").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    on = data.get("on") if isinstance(data, dict) else None
    return on if isinstance(on, bool) else None


def save_name_gate(on: bool) -> bool:
    """Persist the name-gate choice. Non-bools are ignored, not written."""
    if not isinstance(on, bool):
        return False
    try:
        path = state_path("listen_name_gate")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"on": on}), encoding="utf-8")
        return True
    except OSError:
        return False


def load_music_ticker() -> bool | None:
    """The persisted 'Show song ticker' tray choice (docs/SPOTIFY_NOTES.md),
    or None when never chosen — then desktop.music.ticker from the config
    decides (never raises)."""
    try:
        data = json.loads(state_path("music_ticker").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    on = data.get("on") if isinstance(data, dict) else None
    return on if isinstance(on, bool) else None


def save_music_ticker(on: bool) -> bool:
    """Persist the song-ticker choice. Non-bools are ignored, not written."""
    if not isinstance(on, bool):
        return False
    try:
        path = state_path("music_ticker")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"on": on}), encoding="utf-8")
        return True
    except OSError:
        return False
