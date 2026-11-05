"""Which Firefox window is the CURRENT tab? (owner report 2026-11-05)

Firefox's own last-focused window is not the truth on a multi-monitor
desktop: the owner can be typing in a window that Firefox considers "last
active in another window's place". The compositor is the truth — on
Hyprland `hyprctl -j clients` carries `focusHistoryID` (0 = the currently
focused window, larger = less recently focused) plus a monitor id per
window, and every Firefox window's title is
`<active tab title> — Mozilla Firefox` (hyphen variants and a Private
Browsing suffix exist). Sway/KDE/GNOME report only which window is focused
now, so recency beyond that falls back to the extension's own order.

This module turns (extension windows, compositor windows) into one ordered
list: matched by normalized active-tab title (exact, then prefix —
compositor titles are capped at 120 chars by clean_title), tie-broken by
compositor focus recency, then by the extension's own order. No compositor
match at all -> the extension's order stands.
"""
from __future__ import annotations

import re

# Firefox window titles: "<tab> - Mozilla Firefox", "<tab> — Mozilla
# Firefox", private: "<tab> — Mozilla Firefox Private Browsing" (also
# "(Private Browsing)" bracket forms on some locales/builds).
_FIREFOX_SUFFIX = re.compile(
    r"""\s*[-–—]\s*Mozilla\s+Firefox\b
        \s*((?:(?:[\(\[]\s*)?
               (?:Private\s+Browsing|Privat(?:es|e)?\s+(?:Browse|Browsing))
               \s*(?:[\)\]])?\s*))?$""",
    re.I | re.X)
# classes that browse the web with our extension (thunderbird shares the
# desktop-sense firefox-family regex but never titles like this)
FF_CLASS_RE = re.compile(r"firefox", re.I)

_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
              "six": "6", "seven": "7", "eight": "8", "nine": "9",
              "ten": "10", "first": "1", "second": "2", "third": "3",
              "fourth": "4", "fifth": "5"}


def split_firefox_title(title: str) -> tuple[str, bool]:
    """'<tab> — Mozilla Firefox [Private Browsing]' -> (tab title, private).
    Titles without the suffix come back unchanged with private=False."""
    t = " ".join(str(title or "").split())
    m = _FIREFOX_SUFFIX.search(t)
    if not m:
        return t, False
    base = t[:m.start()].rstrip(" -–—\t")
    return base, bool((m.group(1) or "").strip())


def normalize_title(title: str) -> str:
    """Casefold, collapse whitespace and unify dash forms for matching."""
    t = " ".join(str(title or "").split()).casefold()
    return re.sub(r"[\u2010-\u2015]", "-", t)


def _title_score(a: str, b: str) -> int:
    """3 exact, 2 prefix (compositor titles are truncated at 120 chars,
    so one side may be a strict prefix), 0 no match."""
    if not a or not b:
        return 0
    if a == b:
        return 3
    if (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 20:
        return 2
    return 0


def _recency_key(comp) -> int:
    """Compositor focus recency (lower = more recent); None recency sorts
    last within the matched group, and unmatched rows sort after all
    matched ones (big value)."""
    if comp is None:
        return 10 ** 6
    fr = comp.get("focus_recency")
    if isinstance(fr, int):
        return fr
    return 0 if comp.get("focused") else 10 ** 5


def match_windows(ext_windows: list[dict], comp_windows: list[dict]):
    """Pair extension windows with compositor windows by active-tab title.

    ext_windows: [{title, tab_count, ...}] already in the extension's own
    most-recently-focused order (or arbitrary for the listTabs fallback).
    comp_windows: backend rows (raw, unfiltered); only firefox-class rows
    are candidates; their titles get the Firefox suffix stripped.

    Returns a list of (ext, comp_or_None) pairs ordered by compositor
    focus recency (focusHistoryID / focused) first, the extension's order
    second — that order IS the answer to "which tab is current": index 0
    is the most recently focused Firefox window on the compositor."""
    cands = []
    for c in comp_windows or []:
        if not c or not FF_CLASS_RE.search(str(c.get("app") or "")):
            continue
        base, priv = split_firefox_title(c.get("title", ""))
        cands.append({"row": c, "norm": normalize_title(base),
                      "private": priv})
    used: set[int] = set()
    pairs: list[tuple[dict, dict | None]] = []
    for ext in ext_windows or []:
        en = normalize_title(ext.get("title", ""))
        best, best_key = None, None
        for i, cand in enumerate(cands):
            if i in used:
                continue
            score = _title_score(en, cand["norm"])
            if not score:
                continue
            # score first; ties break on compositor recency (the freshest
            # window pairs with the freshest extension window), then on
            # the private-window flag matching
            key = (-score, _recency_key(cand["row"]),
                   0 if cand["private"] == bool(ext.get("private")) else 1, i)
            if best_key is None or key < best_key:
                best, best_key = i, key
        if best is None:
            pairs.append((ext, None))
        else:
            used.add(best)
            pairs.append((ext, cands[best]["row"]))
            ext["_comp_private"] = cands[best]["private"]
    pairs.sort(key=lambda p: (_recency_key(p[1]),
                              0 if p[1] is not None else 1))
    return pairs


def parse_workspace_ref(text) -> str | None:
    """'desktop 2' / 'virtual desktop two' / 'workspace 2' / '2' / a name ->
    the compositor's workspace token ('2', 'web'). None when nothing
    recognizable is left."""
    s = str(text or "").strip().lower()
    if not s:
        return None
    m = re.search(r"\d+", s)
    if m:
        return m.group(0)
    for w, n in _NUM_WORDS.items():
        if re.search(rf"\b{w}\b", s):
            return n
    stripped = re.sub(
        r"\b(?:virtual|the|a|my|to|on|screen|switch|go|show|open)\b", " ", s)
    stripped = re.sub(r"\b(?:desktop|workspace)\b", " ", stripped).strip()
    return stripped or None
