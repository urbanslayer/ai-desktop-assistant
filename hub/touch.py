"""Touch reactions on the hub side (docs/TOUCH.md §Hub): the pure half of
POST /api/touch — the gate, the history note, the model instruction. No DB,
no network; hub/server.py wires it to the store, the lock and the LLM.

The fast path answers off this module alone (mode off, cooldown, 10-min
limit, busy, always-silent kind): no LLM call happens for those, which the
tests assert against the fake server.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# contract enums (docs/TOUCH.md)
MODES = ("off", "quiet", "chatty")
KINDS = ("pat", "poke", "tickle", "grab", "hold", "release", "action",
         "sit", "stand")
REASONS = ("llm", "silent_kind", "mode_off", "cooldown", "limit", "busy",
           "llm_error")

# settings defaults (merged_settings / PUT /api/settings / web card).
# docs/TOUCH.md: quiet (and off) default to a 45 s cooldown / 4 per 10 min;
# chatty to 15 s / 12. A saved value always wins over the mode default.
def defaults_for(mode: str) -> dict:
    if mode == "chatty":
        return {"mode": "chatty", "cooldown_s": 15.0, "max_per_10min": 12}
    return {"mode": mode if mode in MODES else "quiet",
            "cooldown_s": 45.0, "max_per_10min": 4}


SETTINGS_DEFAULTS = defaults_for("quiet")
RATE_WINDOW_S = 600          # max_per_10min
HOLD_RELEASE_MS = 3000       # quiet speaks for `release` only after this hold

# kinds that NEVER speak (any mode): a limb grab is the page's own squishy
# play; `action` is a gesture the page already plays on its own.
NEVER_SPEAK = ("grab", "action", "sit", "stand")
# quiet mode speaks only for these (release gated by HOLD_RELEASE_MS)
QUIET_SPEAK = ("pat", "tickle", "release")

# grey system-note text per kind (the trigger line stored in history)
TRIGGER_NOTES = {
    "pat": "patted her head",
    "poke": "poked her",
    "tickle": "tickled her",
    "grab": "grabbed a limb",
    "hold": "held her",
    "release": "let go after holding her",
    "action": "asked her for a gesture",
    "sit": "sat down on the bottom edge of the screen",
    "stand": "stood up",
}

# one-line model instruction (the last user turn describes the touch)
_KIND_LINES = {
    "pat": "The owner just patted your head.",
    "poke": "The owner just poked you.",
    "tickle": "The owner is tickling you.",
    "grab": "The owner grabbed one of your limbs.",
    "hold": "The owner is holding you.",
    "release": "The owner just let go after holding you for a while.",
    "action": "The owner asked you for a gesture.",
    "sit": "You just settled down sitting on the bottom edge of the owner's screen.",
    "stand": "You just stood up from the bottom edge of the owner's screen.",
}


def validate(kind) -> str | None:
    """None when `kind` is a contract action, else the 400 detail text."""
    k = str(kind or "")
    if k not in KINDS:
        return f"unknown touch kind: {k or '(empty)'}"
    return None


def speech_allowed(mode: str, kind: str, held_ms=None) -> bool:
    """Whether this touch may produce a SPOKEN reaction in `mode`
    (docs/TOUCH.md: quiet speaks only pat/tickle/release-after-3s; grab and
    action never speak in any mode)."""
    if mode == "off" or kind in NEVER_SPEAK:
        return False
    if kind == "release":
        try:
            return float(held_ms or 0) > HOLD_RELEASE_MS
        except (TypeError, ValueError):
            return False
    if mode == "chatty":
        return True
    return kind in QUIET_SPEAK           # quiet


def gate(mode, kind, held_ms, *, cooldown_s, last_spoken_iso, now_epoch,
         spoken_in_window, max_per_10min, busy) -> str:
    """The fast decision (no LLM). Returns a REASONS value: 'llm' when the
    server should generate a line, else why she stays silent. Order per
    docs/TOUCH.md: mode -> cooldown -> limit -> busy -> silent kind."""
    if mode == "off":
        return "mode_off"
    if speech_allowed(mode, kind, held_ms):
        now = datetime.fromtimestamp(float(now_epoch), tz=timezone.utc)
        if last_spoken_iso:
            try:
                last = datetime.strptime(
                    str(last_spoken_iso), "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
                if now - last < timedelta(seconds=float(cooldown_s or 0)):
                    return "cooldown"
            except (TypeError, ValueError):
                pass
        if int(max_per_10min or 0) >= 0 and int(spoken_in_window or 0) >= int(max_per_10min):
            return "limit"
        if busy:
            return "busy"
        return "llm"
    return "silent_kind"


def note_text(kind: str, mode: str) -> str:
    """The grey history note for a touch trigger (role=system)."""
    base = TRIGGER_NOTES.get(kind, f"triggered a {kind} touch")
    return f"owner touched: {base} ({mode})"


def reaction_prompt(kind: str, held_ms=None, part=None) -> str:
    """The last user turn for the one-sentence reaction turn. The model gets
    the touch described here; tools are never offered (docs/TOUCH.md)."""
    line = _KIND_LINES.get(kind, f"The owner triggered a {kind} touch.")
    extra = ""
    if part:
        extra += f" (the {str(part).replace('_', ' ')} specifically)"
    if kind in ("hold", "release") and held_ms is not None:
        try:
            extra += f" (held about {float(held_ms) / 1000:.0f} s)"
        except (TypeError, ValueError):
            pass
    return (f"{line}{extra}\n"
            "React in character with ONE short spoken sentence about the "
            "touch itself. No tools, no questions back.")


def system_line() -> str:
    """Appended to the persona's system prompt for a touch reaction turn."""
    return ("You are reacting to a PHYSICAL touch from the owner in real "
            "time. Answer with ONE short in-character sentence (an exclamation "
            "is fine); never narrate the touch back verbatim, never offer "
            "tools, never ask a question. Reply in your normal voice.")


SILENT_LINES = {
    "mode_off": "(says nothing — touch reactions are off)",
    "cooldown": "(says nothing — still in cooldown)",
    "limit": "(says nothing — said enough for now)",
    "busy": "(says nothing — busy mid-turn)",
    "silent_kind": "(stays silent, feeling it)",
    "llm_error": "(says nothing — her head is not answering)",
}


def silent_line(reason: str) -> str:
    """The short stored assistant line for a silently-ignored touch
    (docs/TOUCH.md: meta.silent=true, excluded from the rate limit)."""
    return SILENT_LINES.get(reason, "(says nothing)")


CONTRACT_REASONS = {"mode_off": "off", "cooldown": "cooldown",
                    "busy": "busy"}


def contract_reason(reason: str, mode: str = "quiet") -> str:
    """The reason name docs/TOUCH.md publishes (off|cooldown|quiet|busy);
    our finer reasons (limit, silent_kind, llm_error) pass through."""
    if reason == "silent_kind":
        return "quiet" if mode == "quiet" else reason
    return CONTRACT_REASONS.get(reason, reason)


def touch_reply(reason: str, state_version, *, spoke=False, text="",
                emotion="neutral", actions=None, audio_url=None,
                mode="quiet") -> dict:
    """Response body for BOTH paths (docs/TOUCH.md shape + extras the
    desktop replay needs). `reason` carries the contract name; `detail`
    our finer internal reason."""
    return {"ok": True, "reacted": spoke, "spoke": spoke, "text": text,
            "emotion": emotion, "actions": actions or [],
            "audio_url": audio_url,
            "reason": contract_reason(reason, mode), "detail": reason,
            "state_version": state_version}
