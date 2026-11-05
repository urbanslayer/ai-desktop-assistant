"""One-shot timer tools (docs/TIMERS.md H) — Open WebUI-style scheduled turns.

Three per-turn tools (built like the memory tools: the persona and the hub's
timer plumbing arrive through a ctx dict the server assembles per turn):

* ``timer``         schedule one future turn (relative ``+20s``-style or an
                    RFC 3339 timestamp **with** a timezone)
* ``list_timers``   this persona's open (pending/firing) timers
* ``cancel_timer``  cancel one by id or label

The time parsing rules are the contract's: ``+20s`` / ``20 seconds`` /
``5m`` / ``1h`` / ``2d`` (and ``in 10 seconds``) add to the current time;
RFC 3339 needs an explicit offset/Z (bare local times are rejected — "which
timezone?"). Due times are capped at 30 days ahead and the hub holds at most
50 pending timers, so a runaway model cannot mint an army.

This module owns parsing/labels only; persistence is ``Store`` and the fire
loop is ``hub/scheduler.py``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping
from zoneinfo import ZoneInfo, available_timezones

if TYPE_CHECKING:  # annotations only (registry is imported at call time)
    from .registry import ToolSpec

MAX_PENDING_TIMERS = 50
MAX_DAYS_AHEAD = 30
MIN_SECONDS_AHEAD = 1.0

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_RELATIVE_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|second|secs?|sec|s|minutes?|minute|mins?|min|m|hours?|hour|hrs?|hr|h|days?|day|d)(?![a-z])"
)
_DATEISH = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")
_RFC_TZ = re.compile(r"(Z|z|[+-]\d{2}:?\d{2})$")


def valid_timezone(name: str) -> bool:
    """True for an IANA zone name (the settings-timezone validator)."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        return False
    if name in available_timezones():
        return True
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def default_timezone_name() -> str:
    """The server-side default: the TZ env zone if it is a real IANA name
    (compose sets TZ), else whatever the OS clock reports as local zone,
    else UTC. Blank ``settings.timezone`` falls back here."""
    env = (datetime.now().astimezone().tzname() or "").strip()
    tz_env = _env_tz_name()
    if tz_env and valid_timezone(tz_env):
        return tz_env
    for cand in (env,):
        if cand and valid_timezone(cand):
            return cand
    try:
        ZoneInfo("UTC")
        return "UTC"
    except Exception:
        return ""


def _env_tz_name() -> str:
    import os

    return (os.environ.get("TZ") or "").strip()


def resolve_timezone(saved: str = "") -> "ZoneInfo | None":
    """saved (settings.timezone) → TZ default → UTC. None when zoneinfo has
    no data at all (Windows CI): callers then speak UTC."""
    for cand in ((saved or "").strip(), default_timezone_name(), "UTC"):
        if not cand:
            continue
        try:
            return ZoneInfo(cand)
        except Exception:
            continue
    return None


def parse_timer_time(raw: Any, now: datetime, *,
                     max_days: int = MAX_DAYS_AHEAD) -> datetime:
    """``+20s`` / ``20 seconds`` / ``in 5 minutes`` / ``5m`` / ``1h`` / ``2d``
    or RFC 3339 with an explicit timezone → an aware UTC due time.

    Raises ValueError with a model-readable message; the caller feeds that
    back so she can re-ask instead of guessing a timezone.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("timer_error: 'time' is required — e.g. \"20 seconds\", "
                         "\"5m\", \"1h\", \"2d\" or an RFC 3339 time with a timezone")
    s = raw.strip()
    if _DATEISH.match(s):
        # RFC 3339. A naive timestamp is *refused*: "which timezone?" has no
        # safe answer, and silently picking the hub's zone starts arguments.
        if not _RFC_TZ.search(s):
            raise ValueError(
                f"timer_error: {raw!r} has no timezone — RFC 3339 needs an offset "
                'or Z (e.g. "2026-11-05T14:30:00+00:00"), or use a relative time '
                'like "20 seconds"')
        text = s.replace("Z", "+00:00").replace("z", "+00:00")
        text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"timer_error: could not parse {raw!r} — use RFC 3339 with a timezone "
                '("2026-11-05T14:30:00+00:00") or a relative time ("10 minutes")')
        if dt.tzinfo is None:  # pragma: no cover — regex already demanded a suffix
            raise ValueError(f"timer_error: {raw!r} has no timezone")
        target = dt.astimezone(timezone.utc)
    else:
        cleaned = s.lower().strip()
        cleaned = cleaned.lstrip("+").strip()
        for prefix in ("in ", "after ", "for ", "within "):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        for suffix in (" from now", " from now on", " later", " ahead", " ahead of time"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
        cleaned = cleaned.strip()
        pairs = list(_RELATIVE_TOKEN.finditer(cleaned))
        leftover = _RELATIVE_TOKEN.sub("", cleaned)
        leftover = re.sub(r"[ +]|and", "", leftover)
        if not pairs or leftover:
            raise ValueError(
                f"timer_error: could not understand {raw!r} — use e.g. \"20 seconds\", "
                '"5m", "1h", "2d", "+10s", "in 10 seconds" or an RFC 3339 time with '
                "a timezone")
        delta = timedelta()
        for match in pairs:  # "1h30m" sums its parts
            num, unit = match.group(1), match.group(2)
            delta += timedelta(seconds=float(num) * _UNIT_SECONDS[unit])
        target = now + delta
        if delta.total_seconds() <= 0:
            raise ValueError("timer_error: the timer must be in the future")
    if target <= now:
        raise ValueError("timer_error: that time is in the past — timers fire once, in the future")
    if (target - now).total_seconds() < MIN_SECONDS_AHEAD:
        raise ValueError("timer_error: too soon — the minimum lead time is 1 second")
    if (target - now) > timedelta(days=max_days):
        raise ValueError(f"timer_error: too far out — timers are capped at {max_days} days ahead")
    return target.astimezone(timezone.utc)


def label_from_prompt(prompt: str) -> str:
    words = (prompt or "").split()
    label = " ".join(words[:5])
    if len(label) > 40:
        label = label[:39].rstrip(" ,.;:") + "…"
    return label or "timer"


def to_local(due_utc: datetime, tz: "ZoneInfo | None") -> str:
    """RFC 3339 in the hub's configured zone — what the model must read out."""
    if tz is None:
        return due_utc.astimezone(timezone.utc).isoformat(timespec="seconds")
    return due_utc.astimezone(tz).isoformat(timespec="seconds")


def due_fields(due_utc: datetime, tz: "ZoneInfo | None") -> dict:
    return {
        "due_at": due_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "due_at_local": to_local(due_utc, tz),
    }


# ── asked-for request matcher (docs/TIMERS.md "Reviewer additions") ─────────
#
# Same failure mode as body actions (docs/BODY.md §3): at the owner's
# temperature the model often ANSWERS "remind me in 20 seconds to stretch"
# as if it had set the timer without calling the `timer` tool at all. The
# hub therefore keeps a conservative matcher over the USER message: it only
# fires on explicit set-a-timer requests that carry a time, never on
# questions about timers, past-tense mentions, negations ("don't remind
# me"), cancellations or hypotheticals. A match (1) hints the model to call
# the tool this turn and (2) lets the hub create the timer itself if the
# turn ends without one (the asked-for fallback, server.py).

MAX_CHAIN_DEPTH = 3   # chained timers deeper than this are refused

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fourty": 40, "fifty": 50, "sixty": 60, "ninety": 90,
}
_HALF = r"half\s+(?:an?\s+)?hour"
_NUM = r"(?:\d+(?:[.,]\d+)?|" + \
       "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|day|s|m|h|d)(?![a-z])"
_PAIR = rf"(?:{_HALF}|{_NUM}\s*{_UNIT})"
_DUR = rf"{_PAIR}(?:\s*(?:and\s+)?{_PAIR})?"
_AT = r"\d{1,2}[:.]\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?"
_TASK = r"[^.?!;\n]{1,120}"
_SETV = r"(?:set|start|make|add|create|want|need|put)"
_BOUND = r"(?:^|[.!?:;,]\s+)"
# Politeness / discourse words may open the request: "please remind me…",
# "so, remind me…", "could you remind me…".
_POLITE_PREFIX = (r"(?:(?:so|and|then|also|but|just|now|okay|ok|please|pls"
                  r"|hey|um|uh|kindly|could\s+you|can\s+you|will\s+you"
                  r"|would\s+you|pls)\s+)*")
# Label candidates never start with a determiner ("my timer for 20 minutes
# is done" is a mention, not an ask).
_LABEL = r"(?!(?:my|the|this|that|our|your|his|her|a|an)\s)[a-z][a-z0-9 _-]{0,24}?"

_PAIR_RES = re.compile(rf"({_NUM})\s*({_UNIT})", re.IGNORECASE)

# Question words opening the message, or question words naming a timer: the
# owner is ASKING about timers, not setting one.
_NEGATIVES = [
    r"^\s*(?:who|what|when|where|why|how|which|do|does|did|is|are|was|were"
    r"|have|has|tell)\b",
    r"\b(?:what|which|how\s+many|how\s+long|how\s+much|when)\b[^?!]*"
    r"\b(?:timer|reminder|alarm)s?\b",
    r"\bhow\s+long\b|\btime\s+(?:is\s+)?left\b|\bleft\s+on\s+(?:my|the)\b",
    # past-tense mentions: "I set a timer", "you reminded me", "the timer rang".
    # bare "could YOU SET a timer" is a request, so "you" only counts with an
    # explicit past marker.
    r"\b(?:i|we|he|she|they|it|there)\s+(?:just\s+|already\s+|once\s+)?"
    r"(?:set|made|created|started|had|put)\b[^?!]*\b(?:timer|reminder|alarm)s?\b",
    r"\byou\s+(?:just|already|once|earlier|previously)\s+"
    r"(?:set|made|created|started|put)\b",
    r"\breminded\b|\btimers?\s+(?:went\s+off|fired|rang|expired)\b",
    r"\b(?:timer|reminder|alarm)s?\b[^?!]*\b(?:went\s+off|fired|rang|expired"
    r"|is\s+done|is\s+up|was\s+set)\b",
    r"\b(?:earlier|yesterday|before)\b[^?!]*\b(?:timer|reminder)s?\b",
    # negations and cancellations
    r"\b(?:don'?t|dont|do\s+not|never\s+mind|no)\b[^?!]*"
    r"\b(?:remind|timer|reminder|alarm)\b",
    r"\b(?:cancel|cancell?ed?|delete|remove|stop|clear|kill|snooze)\b[^?!]*"
    r"\b(?:timer|reminder|alarm)s?\b",
    # hypotheticals: "if I asked you to remind me…", "what if…"
    r"\bif\s+i\b|\bwhat\s+if\b|^\s*(?:suppose|imagine)\b",
]
_NEGATIVE_RES = [re.compile(p, re.IGNORECASE) for p in _NEGATIVES]

# Explicit request forms. Every one carries a duration or an at-time; a bare
# "remind me" without a time is NOT a timer request (the hub will not guess).
_REQUEST_RES: list[tuple[str, re.Pattern]] = [
    ("remind", re.compile(
        _BOUND + _POLITE_PREFIX + rf"remind\s+(?:me|us)\s+in\s+(?P<dur>{_DUR})"
        rf"(?:\s*,?\s*(?:to|that|about)\s+(?P<task>{_TASK}))?", re.IGNORECASE)),
    ("remind", re.compile(
        _BOUND + _POLITE_PREFIX + rf"remind\s+(?:me|us)\s+to\s+"
        rf"(?P<task>{_TASK}?)\s+in\s+(?P<dur>{_DUR})", re.IGNORECASE)),
    ("remind", re.compile(
        _BOUND + _POLITE_PREFIX + rf"remind\s+(?:me|us)\s+"
        rf"(?:today\s+|tonight\s+|tomorrow\s+)?at\s+(?P<at>{_AT})"
        rf"(?:\s*(?:to|that|about)\s+(?P<task>{_TASK}))?", re.IGNORECASE)),
    ("remind", re.compile(
        rf"(?:^|[.!?:;]\s+){_POLITE_PREFIX}in\s+(?P<dur>{_DUR})\s*,?\s*"
        rf"remind\s+(?:me|us)(?:\s+to\s+(?P<task>{_TASK}))?", re.IGNORECASE)),
    ("timer", re.compile(
        rf"\b{_SETV}\s+(?:up\s+)?(?:a|an|another|my|the)\s+"
        rf"(?P<kind>timer|reminder|alarm)s?\s+for\s+(?P<dur>{_DUR})"
        rf"(?:\s+(?:called|cawed|labell?ed|named|as|for|to)\s+"
        rf"(?P<label>{_TASK}))?", re.IGNORECASE)),
    ("timer", re.compile(
        rf"(?:{_BOUND}(?:{_SETV}\s+(?:up\s+)?(?:a|an|the|my)\s+)?"
        rf"|(?:\b{_SETV}\s+(?:up\s+)?(?:a|an|the|my)\s+))"
        rf"(?P<dur>{_DUR})\s+(?:(?P<label>{_LABEL})\s+)?"
        rf"(?:timer|reminder|alarm)\b"
        rf"(?:\s+for\s+(?P<for>[^.?!;\n]{{1,60}}))?", re.IGNORECASE)),
    ("timer", re.compile(
        _BOUND + rf"(?:(?:{_SETV}\s+(?:up\s+)?(?:a|an)\s+)|(?:a|an)\s+)?"
        rf"(?:(?P<label>{_LABEL})\s+)?"
        rf"(?:timer|reminder|alarm)s?\s+for\s+(?P<dur>{_DUR})"
        rf"(?:\s+(?:called|labell?ed|named|as)\s+(?P<label2>[^.?!;\n]{{1,60}}))?",
        re.IGNORECASE)),
    ("wake", re.compile(
        _BOUND + _POLITE_PREFIX + rf"wake\s+me\s+(?:up\s+)?in\s+(?P<dur>{_DUR})"
        rf"(?:\s+to\s+(?P<task>{_TASK}))?", re.IGNORECASE)),
    ("wake", re.compile(
        _BOUND + _POLITE_PREFIX + rf"wake\s+me\s+(?:up\s+)?at\s+(?P<at>{_AT})",
        re.IGNORECASE)),
]

# A reply that clearly refused: the hub must not create the timer anyway.
_REFUSAL = re.compile(
    r"\b(?:can(?:not|'?t)|couldn'?t|could\s+not|wasn'?t\s+able|am\s+not\s+able"
    r"|not\s+able|unable|won'?t|will\s+not|refus\w*|declin\w*)\b",
    re.IGNORECASE)

# Trailing politeness that must not ride into the label/prompt.
_POLITE = re.compile(r"[\s,]*(?:please|pls|thanks|thank\s+you|ty|kthx|now"
                     r"|ok|okay|alright|for\s+me)\b[\s.!]*$", re.IGNORECASE)


def _clean_phrase(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip())
    for _ in range(3):
        text = _POLITE.sub("", text).strip()
    return text.strip(" ,.;:'\"-").strip()


def _dur_seconds(raw: str) -> float | None:
    """Seconds in a duration phrase ("20 seconds", "1h30m", "twenty minutes",
    "half an hour"), None when nothing parses."""
    text = re.sub(_HALF, "0.5h", str(raw or ""), flags=re.IGNORECASE)
    total = 0.0
    matched_any = False
    for num, unit in _PAIR_RES.findall(text):
        num, unit = num.lower().strip(), unit.lower().strip()
        if unit not in _UNIT_SECONDS:
            continue
        try:
            value = float(num.replace(",", "."))
        except ValueError:
            value = float(_NUM_WORDS.get(num, 0))
        if value <= 0:
            continue
        total += value * _UNIT_SECONDS[unit]
        matched_any = True
    return total if matched_any else None


def _resolve_at(raw_at: str, now: datetime, tz_name: str) -> datetime | None:
    """'HH:MM' / '7.30 pm' in the owner's zone → aware datetime: today, or
    tomorrow when it already passed. None when the clock value is bogus."""
    text = str(raw_at or "").lower().strip().replace(".", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    try:
        zone = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    local_now = now.astimezone(zone)
    try:
        target = local_now.replace(hour=hour, minute=minute,
                                   second=0, microsecond=0)
    except ValueError:
        return None
    if target <= local_now:
        target += timedelta(days=1)
    return target


def detect_timer_request(text: str, now: datetime | None = None,
                         tz_name: str = "") -> dict | None:
    """The timer the USER message explicitly asks for, or None.

    Returns {"time", "label", "task", "prompt"} where `time` is ready for
    ``parse_timer_time`` (a normalized duration like "1200s" — word numbers
    the model's parser does not speak are converted — or an RFC 3339 time
    WITH the owner's timezone offset for the at-HH:MM forms). Conservative:
    questions about timers, past-tense mentions, negations, cancellations
    and hypotheticals never match; a request without a time never matches.
    """
    raw = str(text or "")
    if not raw.strip():
        return None
    for neg in _NEGATIVE_RES:
        if neg.search(raw):
            return None
    now = now or datetime.now(timezone.utc)
    for kind, pat in _REQUEST_RES:
        m = pat.search(raw)
        if not m:
            continue
        groups = m.groupdict()
        task = _clean_phrase(groups.get("task") or "")
        label = _clean_phrase(groups.get("label") or groups.get("label2")
                              or groups.get("for") or "")
        time_raw = ""
        if groups.get("dur"):
            secs = _dur_seconds(groups["dur"])
            if not secs or secs < MIN_SECONDS_AHEAD:
                continue
            time_raw = f"{secs:g}s"
        elif groups.get("at"):
            target = _resolve_at(groups["at"], now, tz_name)
            if target is None:
                continue
            time_raw = target.isoformat(timespec="seconds")
        else:
            continue
        if kind == "remind":
            task = task or "check in with the owner"
            label = label or task
            prompt = f"Reminder: {task}"
        elif kind == "wake":
            task = task or "wake the owner up"
            label = label or "wake up"
            prompt = f"Reminder: {task}"
        else:
            label = label or "timer"
            prompt = f'Timer "{label}" is up.'
        return {"time": time_raw, "label": label[:80], "task": task,
                "prompt": prompt[:4000]}
    return None


def request_hint(req: dict) -> str:
    """One-line prompt nudge for a turn whose user message asked for a timer."""
    return (f'The user just asked for a timer: call the timer tool '
            f'(time="{req["time"]}", label="{req["label"]}", '
            f'prompt="{req["prompt"]}") before replying.')


def reply_refuses(text: str) -> bool:
    """True when the reply clearly refuses the ask (fallback must stand down)."""
    return bool(_REFUSAL.search(str(text or "")))


CANCEL_EVENTS = ("chat.user_message", "chat.read")


def validate_cancel_on(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError('timer_error: "cancel_on" must be a list, e.g. ["chat.user_message"]')
    out = []
    for ev in raw:
        ev = str(ev or "").strip()
        if ev not in CANCEL_EVENTS:
            raise ValueError(
                f"timer_error: unknown cancel_on event {ev!r} — only "
                f"{', '.join(CANCEL_EVENTS)} exist")
        if ev not in out:
            out.append(ev)
    return out


def build_timer_specs(store: Any, persona_id: str,
                      timers_api: Mapping[str, Callable[..., Any]],
                      snapshot: Callable[[], dict]) -> list[ToolSpec]:
    """timer / list_timers / cancel_timer for one turn (``timers_api`` binds
    the hub's create/cancel helpers + the effective timezone — see server).
    ``snapshot()`` returns the fired-turn capture (merged settings + offered
    tool ids) as it stands when the tool actually runs."""
    from .registry import HUB_TOOL_DESCRIPTIONS as D, ToolSpec

    def run_timer(args: dict) -> str:
        cancel_on = validate_cancel_on(args.get("cancel_on"))
        create = timers_api["create"]
        try:
            row = create(persona_id=persona_id,
                         prompt=str(args.get("prompt") or "").strip(),
                         time_raw=args.get("time"),
                         label=str(args.get("label") or "").strip(),
                         cancel_on=cancel_on, snapshot=snapshot())
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        return json.dumps({
            "id": row["id"],
            "label": row["label"],
            "due_at": row["due_at"],
            "due_at_local": row["due_at_local"],
            "timezone": row["timezone"],
            "local_time": row["local_time"],
            "cancel_on": row["cancel_on"],
            "note": "Read the local_time back to the user. The hub fires this as "
                    "a chat turn when it is due; never-interrupt applies.",
        }, ensure_ascii=False)

    def run_list(_args: dict) -> str:
        rows = []
        for r in store.list_timers(persona_id=persona_id):
            if r["status"] not in ("pending", "firing"):
                continue
            item = {"id": r["id"], "label": r["label"], "status": r["status"],
                    "due_at": r["due_at"], "cancel_on": r["cancel_on"]}
            local = timers_api.get("local")
            if local:
                try:
                    item["due_at_local"] = local(r["due_at"])
                except (ValueError, KeyError):
                    pass
            rows.append(item)
        return json.dumps({"timers": rows}, ensure_ascii=False)

    def run_cancel(args: dict) -> str:
        tid = args.get("id")
        label = str(args.get("label") or "").strip()
        if tid in (None, "") and not label:
            return json.dumps({"error": "timer_error: give id or label"},
                              ensure_ascii=False)
        out = timers_api["cancel"](persona_id, tid, label)
        return json.dumps(out, ensure_ascii=False)

    def _label_timer(args: dict) -> str:
        return "setting a timer: " + str((args or {}).get("label")
                                         or (args or {}).get("prompt") or "")[:40]

    return [
        ToolSpec(
            name="timer", display="timer", description=D["timer"],
            parameters={
                "type": "object", "properties": {
                    "prompt": {"type": "string",
                               "description": "the instruction to run when the timer fires"},
                    "time": {"type": "string",
                             "description": 'when to fire: relative ("20 seconds", "+10s", '
                                            '"5m", "1h", "2d", "in 10 minutes") or RFC 3339 '
                                            'WITH timezone ("2026-11-05T14:30:00+00:00")'},
                    "label": {"type": "string",
                              "description": "short human name (optional)"},
                    "cancel_on": {"type": "array", "items": {"type": "string"},
                                  "description": 'events that cancel it: "chat.user_message", '
                                                 '"chat.read"'},
                },
                "required": ["prompt", "time"],
            },
            source="hub", risk="act", run=run_timer, label=_label_timer),
        ToolSpec(
            name="list_timers", display="list_timers", description=D["list_timers"],
            parameters={"type": "object", "properties": {}},
            source="hub", risk="read", run=run_list,
            label=lambda args: "checking the timers"),
        ToolSpec(
            name="cancel_timer", display="cancel_timer", description=D["cancel_timer"],
            parameters={
                "type": "object", "properties": {
                    "id": {"type": "integer", "description": "timer id"},
                    "label": {"type": "string", "description": "timer label (id wins)"},
                },
            },
            source="hub", risk="act", run=run_cancel,
            label=lambda args: "cancelling a timer"),
    ]
