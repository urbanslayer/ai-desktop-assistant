"""Monitor phrase resolver (docs/DESKTOP_SENSE.md: 'the left/middle/right
screen', 'the Dell on the right', 'the portable screen', exact names).

Monitors come in the contract shape: {name, description, size, position,
scale, focused?, active_workspace?}. `position` is 'XxY'; ordering by x
gives left→right. The live state is the truth; config naming (from the Lua
reader) only enriches descriptions.
"""
from __future__ import annotations

import re

from .util import parse_position

# words that pin the spot in the layout
_LEFT = ("left", "leftmost", "left-hand", "west")
_RIGHT = ("right", "rightmost", "right-hand", "east")
_MIDDLE = ("middle", "center", "centre", "mid")
_MAIN = ("main", "primary", "built-in", "builtin", "laptop")
_STOP = {"the", "a", "an", "on", "of", "my", "screen", "monitor", "display",
         "one", "ones", "to", "in", "and", "please", "it", "is"}
# 'portable' is not in an EDID string; map it through known portable-panel
# brands so "the portable screen" finds the ARZOPA-style panel.
_PORTABLE_WORDS = ("portable", "mobile", "travel")
_PORTABLE_BRANDS = ("arzopa", "uperfect", "portratio", "innocn", "mego",
                    "tripletop", "espresso")


def _desc_words(monitor: dict) -> set[str]:
    hay = " ".join(str(monitor.get(k) or "") for k in
                   ("name", "description", "make", "model"))
    return {w for w in re.split(r"[^a-z0-9]+", hay.lower()) if w and w not in _STOP}


def _ordered(monitors):
    return sorted(monitors or [], key=lambda m: parse_position(m.get("position")))


def _pick_position(pool, words):
    """left/right/middle/main among `pool` -> (monitor | None, why)."""
    ordered = _ordered(pool)
    if not ordered:
        return None, "no monitors"
    if any(w in words for w in _LEFT):
        return ordered[0], ""
    if any(w in words for w in _RIGHT):
        return ordered[-1], ""
    if any(w in words for w in _MIDDLE):
        if len(ordered) % 2 == 1:
            return ordered[len(ordered) // 2], ""
        return None, (f"'middle' needs an odd number of screens; there "
                      f"{len(ordered)}: " + _names(ordered))
    if any(w in words for w in _MAIN):
        focused = [m for m in ordered if m.get("focused")]
        return (focused or ordered)[0], ""
    return None, None  # no position word present


def _names(monitors):
    return ", ".join(str(m.get("name") or "?") for m in monitors)


def resolve_monitor_phrase(phrase, monitors, backend_name=""):
    """Resolve a human monitor phrase to ONE live monitor dict.

    Returns (monitor, None) on a confident match or (None, error message)
    that is safe to hand to the model. A bare monitor name also works;
    when nothing is asked for, callers pass None themselves (no default).
    """
    monitors = [m for m in (monitors or []) if m.get("name")]
    if not monitors:
        return None, "this desktop reports no monitors"
    text = str(phrase or "").strip().lower()
    if not text:
        return None, "empty monitor phrase"
    words = {w for w in re.split(r"[^a-z0-9]+", text) if w and w not in _STOP}

    # 1. an exact output name anywhere in the phrase
    exact = [m for m in monitors
             if str(m["name"]).lower() in text.split() or
             text in (str(m["name"]).lower(), str(m.get("description") or "").lower())]
    if len(exact) == 1:
        return exact[0], None

    # 2. description/name word filter ("dell", "arzopa", "u2518d", "hdmi-a-1")
    wordy = []
    for m in monitors:
        dw = _desc_words(m)
        hits = sum(1 for w in words if w in dw)
        if hits:
            wordy.append((hits, m))
    # 'portable' is not in EDIDs; treat it as a brand hint (ARZOPA etc.)
    if any(w in words for w in _PORTABLE_WORDS):
        words = words | set(_PORTABLE_BRANDS)
    if any(w in words for w in _PORTABLE_BRANDS):
        extra = [m for m in monitors
                 if any(b in _desc_words(m) or b in
                        str(m.get("description") or "").lower()
                        for b in _PORTABLE_BRANDS)]
        if extra:
            wordy = [(h + 3, m) for h, m in wordy if m in extra] or \
                    [(3, m) for m in extra]

    pool = monitors
    if wordy:
        best = max(h for h, _ in wordy)
        pool = [m for h, m in wordy if h == best]
        if len(pool) > 1:
            mon, why = _pick_position(pool, words)
            if mon:
                return mon, None
            # "the Dell" with two Dells and no position word: only guess if
            # their live positions differ enough to name one (left/right edge)
            ordered = _ordered(pool)
            if len(pool) == 2:
                return None, (f"'{phrase}' matches two screens ({_names(pool)}); "
                              "say left or right")
            return None, (f"'{phrase}' matches several screens ({_names(pool)}"
                          + (f"; {why}" if why else "") + ")")
        return pool[0], None

    # 3. pure position words
    mon, why = _pick_position(monitors, words)
    if mon:
        return mon, None
    if why:
        return None, why
    return None, (f"I don't know a screen called {phrase!r}; I see: "
                  + _names(_ordered(monitors))
                  + (" (descriptions: "
                     + "; ".join(str(m.get("description") or "-")
                                 for m in _ordered(monitors)) + ")"
                     if any(m.get("description") for m in monitors) else ""))
