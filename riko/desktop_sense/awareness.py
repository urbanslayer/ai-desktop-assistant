"""The privacy gate (docs/DESKTOP_SENSE.md "Privacy rules").

Master toggle `desktop.awareness: on | off` in character_config.yaml seeds
the INITIAL value; the tray checkbox "Let her see my desktop" overrides it
at runtime and the choice is persisted next to the listening mode
(~/.config/riko/desktop_awareness.json, $RIKO_APP_STATE_DIR for tests) —
same pattern as riko/appstate.py. Default is OFF: she sees nothing until
the owner turns it on, and while it is off the desktop_* tools are not
registered on the hub at all.

Denylist: app ids / title regexes that are never reported. Defaults cover
password managers and private/incognito browser windows; extra entries go
in `desktop.awareness.hide`. A hidden window is reported as just
{"app": "<hidden>"} in window lists (and dropped from the focused answer).
"""
from __future__ import annotations

import json
import os
import re

from riko.appstate import state_path

# Password managers we never name or title (matched against app id/class,
# lowercase substring) — the contract's default denylist.
DEFAULT_HIDE_APPS = [
    "keepassxc", "keepass", "keeweb", "bitwarden", "1password", "lastpass",
    "seahorse", "kwallet", "plasma-vault", "passky", "vault",
]
# ...and against window titles (regexes). Private/incognito browsing is in
# here: Firefox "… — Private Browsing", Chrome/Chromium "…Incognito Window",
# Edge "InPrivate", plus the Hyprland/Sway class-level markers.
DEFAULT_HIDE_TITLES = [
    r"private\s+browsing", r"incognito\s+window", r"privat[ei]nse\s+window",
    r"隐身窗口",  # Chrome zh-CN incognito
]
PRIVATE_URL_RE = re.compile(r"^(moz-action:privatebrowsing|about:privatebrowsing)", re.I)

_TRUE = ("on", "true", "yes", "1", "enabled")
_FALSE = ("off", "false", "no", "0", "disabled")


def _as_bool(value, default=None):
    if isinstance(value, bool):
        return value
    s = str(value or "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


class Awareness:
    """The live toggle + denylist. `cfg` is the parsed character_config.yaml."""

    STATE_NAME = "desktop_awareness"

    def __init__(self, cfg=None):
        d = (cfg or {}).get("desktop") or {}
        seed = d.get("awareness")
        # `awareness: off` (scalar) or `awareness: {enabled: off, hide: […]}`
        hide = []
        if isinstance(seed, dict):
            hide = seed.get("hide") or []
            seed = seed.get("enabled", seed.get("on"))
        self._default = bool(_as_bool(seed, False))
        self._extra_hide = [str(h) for h in hide if str(h).strip()]
        self._enabled = None  # resolved lazily: persisted choice wins

    # ── the toggle ─────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        if self._enabled is None:
            stored = self._load()
            self._enabled = self._default if stored is None else stored
        return self._enabled

    def set(self, on: bool) -> bool:
        """Toggle + persist (like listen mode). Returns the new state."""
        self._enabled = bool(on)
        try:
            path = state_path(self.STATE_NAME)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"enabled": self._enabled}),
                            encoding="utf-8")
        except OSError:
            pass  # the in-memory toggle still stands for this run
        return self._enabled

    @staticmethod
    def _load():
        try:
            data = json.loads(state_path(Awareness.STATE_NAME)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return _as_bool(data.get("enabled"), None)

    # ── the denylist ───────────────────────────────────────────
    def hide_apps(self) -> list[str]:
        """App-id substrings that are never reported (contract default +
        the owner's extra entries, which may also be title regexes)."""
        apps = list(DEFAULT_HIDE_APPS)
        titles = [re.compile(p, re.I) for p in DEFAULT_HIDE_TITLES]
        for extra in self._extra_hide:
            # an entry with regex punctuation counts as a title pattern;
            # plain words go on the app list (cheap, predictable heuristic)
            if re.search(r"[\[\](){}*+?|^$\\]", extra):
                titles.append(re.compile(extra, re.I))
            else:
                apps.append(extra.lower())
        self._title_res = titles
        return apps

    @property
    def title_res(self):
        if not hasattr(self, "_title_res"):
            self.hide_apps()
        return self._title_res

    def hides(self, app: str = "", title: str = "") -> bool:
        """True when this window must not be named (denylist hit). App-id
        substrings also match titles: keepassxc windows often say the class
        nowhere and "KeePassXC" only in the caption."""
        app_l = str(app or "").lower()
        apps = self.hide_apps()
        if any(h in app_l for h in apps if h):
            return True
        t = str(title or "")
        t_l = t.lower()
        if any(h in t_l for h in apps if h):
            return True
        return any(rx.search(t) for rx in self.title_res)

    def hides_private(self, title: str = "", url: str = "") -> bool:
        """Private/incognito browser windows/tabs (default-hidden; the URL
        arm covers tabs, where the title alone may be plain)."""
        if self.hides("", title):
            return True
        return bool(PRIVATE_URL_RE.match(str(url or "")))
