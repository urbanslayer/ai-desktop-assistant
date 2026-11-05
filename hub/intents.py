"""Hub-side intent matchers: the weather pre-fetch + the promised-action guard.

Two deterministic matchers the chat turn consults so answers are grounded
and promises are kept, even when the model (owner temperature 1.0) talks
about acting instead of acting (docs/API.md "Weather pre-fetch" +
"Promised-action guard"):

* ``detect_weather_request`` — is this USER message a question about the
  weather? Conservative like ``timers.detect_timer_request``: only direct
  questions/requests about current or future conditions match; talking
  ABOUT the weather generally, idioms ("under the weather", "weather the
  storm"), past weather, questions about the weather *tool*, and
  timer/reminder asks never match. A match carries what the weather tool
  needs: an explicit place if the message names one, `days`
  (1=today..7=week) and `hourly` (tonight/later). The hub then fetches the
  data itself and injects it — one LLM round-trip, no tool-call coin flip.

* ``promises_action`` — does the FINAL reply announce an action it did not
  take ("let me check", "I'll look that up", "one moment", …)? Only checked
  when the turn made zero tool calls while tools were offered; the server
  then re-runs the model ONCE with a nudge and keeps the real answer.

Both matchers are pure functions over strings (no network, no state) so
the test tables drive them directly.
"""
from __future__ import annotations

import datetime
import json
import re

# ── weather question matcher ────────────────────────────────────────────────

_MAX_DAYS = 7

# A weather positive only counts inside a question/request context: a '?'
# or one of these discourse words. "I hate this weather" stays talk.
_GATE = re.compile(
    r"(?:[?？]|\b(?:what|how|will|shall|might|could|check|tell|show|look|find"
    r"|give|know|want|see|is|does|do|should|can|any)\b)", re.IGNORECASE)

_WX_NOUN = r"(?:weather|forecast)"
_RAIN_V = (r"(?:rain|raining|rainfall|showers?|snow|snowing|sleet|hail"
           r"|freeze|freezing|pour|pouring|storm|thunder)")
_CLOTHES = (r"(?:coat|umbrella|jacket|raincoat|anorak|boots|gloves|scarf"
            r"|hat|hoodie|sunscreen|sunglasses|thermals?)")
_COND = r"(?:cold|hot|warm|cool|chilly|freezing|nippy|balmy)"
_SKY = (r"(?:sunny|cloudy|overcast|clear|stormy|windy|foggy|grey|gray"
        r"|breezy|humid|pouring|bright)")
# words that turn a temperature-word question into a weather question
_PLACE_CTX = r"\b(?:outside|out\s+there|out\s+doors|today|tonight|tomorrow|now)\b"

# Never a weather question: idioms, past, the tool itself, timers.
_NEGATIVES = [re.compile(p, re.IGNORECASE) for p in [
    r"\bunder\s+the\s+weather\b", r"\bweather\s+the\s+storm\b",
    r"\bweather(?:proof|proofing|ized|ised)\b",
    # past tense / past anchors: forecasts answer now and forward.
    r"\b(?:was|were)\b[^?!.]{0,60}\b(?:" + _WX_NOUN + r"|temperature"
    r"|raining|snowing|rained|sunny|cold|hot|" + _RAIN_V + r")\b",
    r"\b(?:yesterday|ago)\b|\blast\s+(?:night|week|weekend|month)\b"
    r"|\bhow\s+was\b",
    # questions about the weather tool/app, not about the sky
    r"\bweather\s+(?:tool|app|apps|api|website|site|service|mcp)\b",
    r"\b(?:tool|tools|function|functions)\b[^?!.]{0,40}\b" + _WX_NOUN + r"\b",
    r"\b" + _WX_NOUN + r"[^?!.]{0,40}\b(?:tool|tools|function)\b",
    # timer/other-tool requests that merely mention weather words
    r"\b(?:timer|timers|remind|reminds|reminding|reminder|reminders"
    r"|alarm|alarms)s?\b",
]]

# (pattern, extra_required_pattern | None): the message must match both.
_POSITIVES = [(re.compile(p, re.IGNORECASE),
               re.compile(x, re.IGNORECASE) if x else None)
              for p, x in [
    # "what's the weather", "how's the forecast (for tomorrow)"
    (rf"\b(?:what|how)(?:'s|\u2019s|\s+is|\u2019\s+is)\s+(?:the\s+)?{_WX_NOUN}\b",
     None),
    (rf"\b{_WX_NOUN}\s+(?:forecast\s+)?(?:report|outlook)\b", None),
    # future rain/snow: "will it rain", "it's going to pour", "do you think
    # it will snow", "is it going to freeze", "chance of rain"
    (rf"\b(?:will|shall|might|could)\s+(?:it|there)\b[^?!.]{{0,25}}{_RAIN_V}\b",
     None),
    (rf"\b(?:it|there)(?:'s|\u2019s|\s+is|\s+will|\s+might|\s+could|\s+shall)"
     rf"\b[^?!.]{{0,25}}{_RAIN_V}\b", None),
    (rf"\bis\s+it\s+(?:going\s+to|gonna)\s+{_RAIN_V}\b", None),
    (rf"\bis\s+it\s+(?:raining|snowing|pouring|hailing|sleet(?:ing)?"
     rf"|drizzling|storming)\b", None),
    (r"\bchance\s+of\s+(?:rain|rainfall|snow|showers|precipitation|thunder)\b",
     None),
    # coat/umbrella questions
    (rf"\b(?:do|should|could|will|shall|might)\s+i\s+(?:really\s+|actually\s+)?"
     rf"(?:need|bring|take|wear|pack|carry|grab)\b[^?!.]{{0,25}}{_CLOTHES}\b",
     None),
    (rf"\bi\s+need\s+an?\s+(?:umbrella|coat|raincoat|jacket|boots)\b", None),
    # "how hot is it", "how cold will it be"
    (rf"\bhow\s+(?:hot|cold|warm|cool|chilly|freezing)\s+"
     rf"(?:is|'s|\u2019s|will|does|might|would)\s+(?:it|outside|today|tomorrow"
     rf"|the\s+(?:weather|temperature))\b", None),
    # temperature needs a present/outside context word ("of the oven" is not
    # a weather question)
    (r"\btemperature\b", rf"{_PLACE_CTX}|\bin\s+\w"),
    # bare weather/forecast questions without a set phrase: "weather in
    # Tokyo tomorrow?" — ends with '?' or carries an explicit ask verb, so
    # statements and small talk ("what's your favorite weather") stay out
    (rf"\b{_WX_NOUN}\b", r"\?\s*$|\b(?:check|tell|show|look|find"
     r"|give|know|want|wonder)\b"),
    # "what's the weekend forecast" — what's + <anchor> + forecast
    (r"\b(?:what|how)(?:'s|\u2019s|\s+is)\s+(?:the\s+)?[\w'’\s]{0,12}?"
     r"\bforecast\b", None),
    # subjectless asks: "need an umbrella?"
    (rf"\bneed\s+an?\s+(?:umbrella|coat|raincoat|jacket|boots)\b", None),
    # "what's it like outside", "how's it looking out there"
    (r"\bwhat(?:'s|\u2019s|\s+is|s)?\s+it\s+like\s+(?:outside|out\s+there"
     r"|out\s+doors)\b", None),
    (r"\bhow(?:'s|\u2019s|\s+is)\s+it\s+(?:looking\s+)?(?:outside|out\s+there)\b",
     None),
    # "is it raining/sunny/windy" — sky words stand alone; temperature words
    # need an outside/today context ("is it cold in here?" is not weather)
    (rf"\bis\s+it\s+{_SKY}\b", None),
    (rf"\bis\s+it\s+{_COND}\b", _PLACE_CTX),
    # imperatives: "check the weather", "tell me the forecast", "see what
    # the weather is like"
    (rf"\b(?:check|check\s+on|look\s+up|find\s+out|tell\s+me|show\s+me"
     rf"|get\s+me|give\s+me|know|see\s+what)\b[^?!.]{{0,25}}(?:the\s+|out\s+)?"
     rf"{_WX_NOUN}\b", None),
    (r"\b(?:check|see|look)\b[^?!.]{0,15}\bif\b[^?!.]{0,25}"
     rf"(?:raining|rains|rain|snowing|snow|{_COND}|{_SKY}|umbrella|freezing)\b",
     None),
]]

# "weather in <place>": capture the trailing place name, allowing a time
# anchor after it ("in Paris tomorrow?"). Ends of message only — an
# in-clause mid-sentence ("in general", "in the game") is not a place.
_PLACE_RE = re.compile(
    r"\b(?:in|for)\s+(?:the\s+)?"
    r"(?P<place>[a-z][a-z0-9,'’ .-]{1,38}?)"
    r"[\s,]*(?:(?:this|next|the)\s+)?(?P<anchor>weekend|week|morning"
    r"|afternoon|evening|tonight|today|tomorrow|right\s+now|now|please"
    r"|pls|thanks|thx)?"
    r"[\s,]*(?:morning|afternoon|evening)?[\s,]*[?!.]?\s*$", re.IGNORECASE)
_PLACE_NOT = {
    "general", "here", "there", "home", "town", "work", "school", "games",
    "game", "minecraft", "fiction", "movies", "films", "life", "general",
    "detail", "exchange", "short", "advance", "theory", "spite", "line",
    "brief", "place", "person", "my", "your", "the", "area", "country",
}

_TIME_RE = re.compile(r"\b(?:(?P<days>\d+)\s+day)s?\b", re.IGNORECASE)


def _weekend_days(today: datetime.date) -> int:
    """Days from today (inclusive, 1-based) covering Sat+Sun: Mon -> 7,
    Fri -> 2, Sat -> 2, Sun -> 1."""
    return min(_MAX_DAYS, ((5 - today.weekday()) % 7) + 2)


def _scope(text: str, today: datetime.date) -> tuple[int, bool]:
    """(days, hourly) the question asks about. Combining anchors take the
    max days; tonight/later/right now also ask for the hourly summary."""
    t = text.lower()
    days = 1
    if re.search(r"this\s+week(?!end)|the\s+week(?!end)|whole\s+week"
                 r"|week\s+ahead|coming\s+week|next\s+week(?!end)|this\s+month",
                 t):
        days = _MAX_DAYS
    if re.search(r"weekend|saturday|sunday", t):
        days = max(days, _weekend_days(today))
    if re.search(r"few\s+days|couple\s+(?:of\s+)?days|several\s+days"
                 r"|next\s+few|coming\s+days|rest\s+of\s+the\s+week", t):
        days = max(days, 3)
    if re.search(r"day\s+after\s+tomorrow", t):
        days = max(days, 3)
    elif re.search(r"\btomorrow\b|daybreak|next\s+day", t):
        days = max(days, 2)
    m = _TIME_RE.search(t)
    if m and re.search(r"weather|forecast|rain|snow|temperature|outside", t):
        days = max(days, min(_MAX_DAYS, int(m.group("days") or 1)))
    hourly = bool(re.search(r"\btonight\b|later\s+today|\blater\b"
                            r"|this\s+(?:afternoon|evening)"
                            r"|next\s+few\s+hours|coming\s+hours"
                            r"|rest\s+of\s+the\s+(?:day|evening)"
                            r"|right\s+now|currently|at\s+the\s+moment", t))
    return min(_MAX_DAYS, days), hourly


def _extract_place(text: str) -> str | None:
    m = _PLACE_RE.search(text)
    if not m:
        return None
    place = re.sub(r"\s+", " ", (m.group("place") or "")).strip(" ,.'\"")
    if not place or place.lower() in _PLACE_NOT:
        return None
    if re.fullmatch(r"(?:today|tonight|tomorrow|week|weekend|now|morning"
                    r"|afternoon|evening|saturday|sunday|please|thanks|pls"
                    r"|this|the|a|right|outside|out)", place, re.IGNORECASE):
        return None
    # a place never contains time/discourse words ("for tomorrow and this
    # week" would otherwise geocode as a town named that)
    if re.search(r"\b(?:today|tonight|tomorrow|week|weekend|morning"
                 r"|afternoon|evening|now|days?|this|next|and|the|please"
                 r"|thanks)\b", place, re.IGNORECASE):
        return None
    return place


def detect_weather_request(text: str,
                           today: datetime.date | None = None) -> dict | None:
    """The weather fetch the USER message asks for, or None.

    Returns {"place": str|None, "days": 1-7, "hourly": bool}: `place` only
    when the message names one ("weather in Paris?" -> "Paris"; the tool
    geocodes it), days = how many to include (today=1, tomorrow=2, week /
    weekend up to 7), hourly = add the next-12-hours summary (tonight /
    later / right now). Conservative: idioms, past weather, general talk
    about weather, questions about the weather tool, and timer asks never
    match.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    for neg in _NEGATIVES:
        if neg.search(raw):
            return None
    if not _GATE.search(raw):
        return None
    for pat, extra in _POSITIVES:
        if pat.search(raw) and (extra is None or extra.search(raw)):
            days, hourly = _scope(raw, today or datetime.date.today())
            return {"place": _extract_place(raw), "days": days,
                    "hourly": hourly}
    return None


def weather_data_block(payload: dict) -> str:
    """The system-prompt block carrying the pre-fetched live data."""
    place = payload.get("place") or {}
    name = str(place.get("name") or "").strip() or "their area"
    bits = ", ".join(b for b in (str(place.get("admin1") or "").strip(),
                                 str(place.get("country") or "").strip())
                     if b)
    where = f"{name} ({bits})" if bits else name
    return ("Live weather data (fetched just now for " + where + "): "
            + json.dumps(payload, ensure_ascii=False, default=str)
            + ". Answer from this; do not call the weather tool again "
              "unless they ask about another place.")


def prefetch_hint(can_call_tool: bool) -> str:
    """Fallback line when the pre-fetch could not run: send her to the
    tool (or to asking for a place) instead of her memory."""
    if can_call_tool:
        return ("The user asked about the weather and the hub could not "
                "fetch live data this time: call the weather tool NOW "
                "(pass place=... only when they named a place; ask which "
                "place if none is known), then answer from its result — "
                "never invent weather from memory.")
    return ("The user asked about the weather but no live data could be "
            "fetched (no location saved): ask them which place they mean; "
            "never invent weather from memory.")


# ── promised-action guard ───────────────────────────────────────────────────
#
# The model sometimes ENDS a turn with an announcement ("Let me check the
# weather for you, senpai.") and no tool call — the user waits for news
# that never comes. The server checks the FINAL reply of a zero-tool-call
# turn against this conservative list; on a hit it re-runs the model once
# with a nudge (server.py), so the announcement never survives as an answer.

_PROMISE_RES = [re.compile(p, re.IGNORECASE) for p in [
    r"\blet\s+(?:me|us)\s+(?:just\s+|quickly\s+|go\s+)?(?:check|look|search"
    r"|find|verify|confirm)\b",
    # contractions keep their apostrophe mandatory: "ill" is not "i'll"
    r"\bi(?:'|\u2019)l[lI]\s+(?:just\s+|quickly\s+|go\s+)?(?:check|look|search"
    r"|find|verify|see\s+to)\b",
    r"\bi(?:'|\u2019)l[lI]\s+(?:just\s+|quickly\s+)?(?:look\s+up|look\s+into"
    r"|find\s+out|check\s+on|check\s+out)\b",
    r"\bi\s+(?:will|am\s+going\s+to)\s+"
    r"(?:just\s+|quickly\s+|go\s+)?(?:check|look|search|find|verify)\b",
    r"\bi(?:'|\u2019)?m\s+going\s+to\s+"
    r"(?:just\s+|quickly\s+|go\s+)?(?:check|look|search|find|verify)\b",
    r"\bi(?:'|\u2019)?m\s+checking\b",
    r"\bchecking\b(?:\s+on)?\s+(?:it|that|this|now|for\s+(?:you|it|that|the))\b",
    r"\b(?:one|just\s+a|just)\s+(?:quick\s+)?moment\b",
    r"\bgive\s+me\s+a\s+sec(?:ond)?\b",
    r"\b(?:hold|hang)\s+on\b",
    # promised timers (the timer fallback covers creating it; the nudge
    # usually just makes her call the tool like she said)
    r"\bi(?:'|\u2019)l[lI]\s+(?:set|start|put)\b[^.!?\n]{0,24}"
    r"\b(?:timer|reminder|alarm)\b",
    r"\bi(?:'|\u2019)l[lI]\s+pull\b[^.!?\n]{0,24}\bup\b",
    r"\b(?:fetching|searching|looking)\s+(?:it\s+up|that\s+up|now"
    r"|for\s+(?:it|that|the|you))\b",
]]

PROMISE_NUDGE = ("You said you would check but didn't call a tool. Call "
                 "the appropriate tool now, then answer.")


def promises_action(reply: str) -> bool:
    """True when a final reply announces an action it did not take yet
    ('Let me check…', 'One moment', 'I'll look that up'…). Checked by the
    server only on turns that made no tool call while tools were offered."""
    text = str(reply or "")
    return any(p.search(text) for p in _PROMISE_RES)
