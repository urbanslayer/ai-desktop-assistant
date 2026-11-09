"""Optional name gate for always-listen mode (docs/BACKLOG.md Planned 5).

Owner request: "when Furina is set to always listen, can she only respond if
I say her name?" — with two deliberate limits:

* the gate runs on the FINAL transcript, after riko.asr.apply_corrections
  (so 'Farina' has already become 'Furina' when we look at it), and only on
  VAD segments: push-to-talk is intent (you pressed the key), typed text is
  intent, and dictation is handled by the router before the gate is consulted;
* matching is per PERSONA, never hard-coded: the active persona's hub `name`
  + `keywords`, plus configured aliases (`listen.name_aliases`, a mapping
  persona_id -> [aliases]). With `listen.name_gate_any_persona` (default on)
  a DIFFERENT persona's name also opens the gate — the session routes that
  through the hub's persona switch (POST /api/active) before sending the text.

Matching is case- and punctuation-insensitive, whole-word (tokenised), and
fuzzy by reusing riko.asr._similar (difflib/Jaro-Winkler at the same
FUZZY_MIN_RATIO the corrections pass uses), so 'Furrina' counts while real
English words from COMMON_WORD_GUARD only match exactly. Multi-word names
('Mister Sparkles') match as a token window, exact or fuzzy per token. The
name may sit anywhere in the utterance; the FULL text still goes to the hub
so she sees she was addressed.

Conversation window: once the gate opens for an utterance, follow-ups without
any name are accepted for `listen.follow_up_seconds` (default 12 s, 0 = off);
every accepted utterance restarts the window, and a persona switch closes it.
"""
from __future__ import annotations

import os
import re
import time

from riko import appstate, asr

FOLLOW_UP_SECONDS_DEFAULT = 12.0
PERSONA_CACHE_TTL = 60.0        # persona names/keywords change rarely
ENTRY_ACTIVE = "active"
GATE_ENV = "RIKO_NAME_GATE"     # on/off override (tests; wins over appstate)

# Words that open a direct address ('hey Furina ...'). They never count as
# names themselves; they are dropped before matching so the fuzzy pass does
# not compare 'hey' against short names.
VOCATIVES = frozenset(("hey", "hi", "hello", "yo", "ok", "okay", "oh", "oi",
                       "excuse", "psst"))


def listen_opt(cfg, name, default=None):
    """Read a `listen.<name>` option: the dotted top-level yaml key
    (`listen.name_gate:`) or a nested `listen: {<name>: ...}` mapping —
    same shape as riko.asr._asr_opt."""
    value = cfg.get(f"listen.{name}")
    if value is None and isinstance(cfg.get("listen"), dict):
        value = cfg["listen"].get(name)
    return default if value is None else value


def resolve_initial_gate(cfg) -> bool:
    """Should the gate start on? $RIKO_NAME_GATE (tests) > persisted choice
    (~/.config/riko/listen_name_gate.json) > listen.name_gate in the yaml
    ('off' | 'name'; default off)."""
    env = str(os.environ.get(GATE_ENV) or "").strip().lower()
    if env in ("on", "true", "1", "yes", "name"):
        return True
    if env in ("off", "false", "0", "no"):
        return False
    stored = appstate.load_name_gate()
    if stored is not None:
        return stored
    return str(listen_opt(cfg, "name_gate", "off") or "").strip().lower() in (
        "name", "on", "true", "1", "yes")


def follow_up_seconds(cfg) -> float:
    try:
        secs = float(listen_opt(cfg, "follow_up_seconds", FOLLOW_UP_SECONDS_DEFAULT))
    except (TypeError, ValueError):
        secs = FOLLOW_UP_SECONDS_DEFAULT
    return min(max(secs, 0.0), 600.0)


def any_persona_enabled(cfg) -> bool:
    """listen.name_gate_any_persona: recognise OTHER personas' names too and
    switch to whoever was addressed. Default on."""
    value = listen_opt(cfg, "name_gate_any_persona", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("off", "false", "0", "no")
    return bool(value)


def _tokens(text: str) -> list[str]:
    """Lowercase, punctuation-insensitive word tokens (apostrophes inside a
    word are kept: "what's" stays one word)."""
    return re.findall(r"[a-z0-9]+(?:[''\-][a-z0-9]+)*", str(text).lower())


def _word_hit(word: str, target: str) -> bool:
    """One token vs one name word: exact always counts; otherwise fuzzy at
    the corrections layer's ratio, over asr's length floor, and never for
    real English words the corrections fuzzy pass also refuses to rewrite."""
    if word == target:
        return True
    if (len(word) < asr.FUZZY_MIN_LEN or len(target) < asr.FUZZY_MIN_LEN
            or word in asr.COMMON_WORD_GUARD
            or target in asr.COMMON_WORD_GUARD):
        return False
    return asr._similar(word, target) >= asr.FUZZY_MIN_RATIO


def mentions(text: str, names) -> bool:
    """Does `text` address anybody by one of `names` (anywhere in it)?
    Whole-word, case/punctuation-insensitive, fuzzy; multi-word names match a
    token window; leading vocatives ('hey <name>') are skipped."""
    toks = _tokens(text)
    while toks and toks[0] in VOCATIVES:
        toks = toks[1:]
    if not toks:
        return False
    for name in names or ():
        nt = _tokens(name)
        if not nt:
            continue
        if len(nt) == 1:
            if any(_word_hit(w, nt[0]) for w in toks):
                return True
        elif len(toks) >= len(nt):
            for i in range(len(toks) - len(nt) + 1):
                window = toks[i:i + len(nt)]
                if all(_word_hit(w, t) for w, t in zip(window, nt)):
                    return True
    return False


def persona_names(persona, aliases=None) -> list[str]:
    """Everything that answers to one persona: hub name + keywords + the
    aliases configured for its id (deduped, case-insensitive)."""
    names = []
    for n in ([persona.get("name")]
              + [str(k) for k in (persona.get("keywords") or [])]
              + [str(a) for a in (aliases or [])]):
        n = str(n or "").strip()
        if n and n.lower() not in {x.lower() for x in names}:
            names.append(n)
    return names


def alias_map(cfg) -> dict:
    """listen.name_aliases: mapping persona_id -> [aliases]. A plain list is
    read as aliases for the ACTIVE persona (the original single-persona
    shape); strings become one-item lists."""
    raw = listen_opt(cfg, "name_aliases", {})
    if isinstance(raw, dict):
        return {str(k): (v if isinstance(v, list) else [v]) for k, v in raw.items()}
    if isinstance(raw, (list, tuple, str)):
        return {"": (raw if isinstance(raw, list) else [raw])}
    return {}


def build_entries(cfg, listed_personas, active_id) -> list[dict]:
    """Gate entries from the hub's persona list: one per persona, flagged
    active, each with its full set of answering names."""
    amap = alias_map(cfg)
    entries = []
    seen = set()
    for p in listed_personas or ():
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        entries.append({
            "id": pid,
            "name": str(p.get("name") or pid),
            "names": persona_names(p, amap.get(pid)),
            "active": pid == active_id,
        })
    return entries


def find_mention(text: str, entries: list[dict]):
    """(active_match, other_match): the ACTIVE persona wins over any other
    mention so 'Riko, ask Asuka...' talks to Riko, not Asuka."""
    other = None
    for e in entries:
        if mentions(text, e["names"]):
            if e.get("active"):
                return e, None
            if other is None:
                other = e
    return None, other


class FollowWindow:
    """'She answered, so the next few seconds are still the conversation':
    open(name) re-arms the window; open() says whether it is still open.
    follow_up = 0 disables it entirely. Not thread-safe by design — the
    session worker thread is the only user."""

    def __init__(self, follow_up: float):
        self.follow_up = follow_up
        self._until = 0.0

    def open(self) -> bool:
        return self.follow_up > 0 and time.monotonic() < self._until

    def restart(self):
        if self.follow_up > 0:
            self._until = time.monotonic() + self.follow_up

    def close(self):
        self._until = 0.0
