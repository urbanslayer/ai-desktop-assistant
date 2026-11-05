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
