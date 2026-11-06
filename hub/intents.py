"""Hub-side intent matchers: the weather pre-fetch + the action guards.

Three deterministic matchers the chat turn consults so answers are grounded
and promises are kept, even when the model (owner temperature 1.0) talks
about acting instead of acting (docs/API.md "Weather pre-fetch",
"Promised-action guard", "Claimed-action guard"):

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

* ``detect_action_request`` + ``claims_completed_action`` — the claimed-
  action twin: the user IMPERATIVELY asked for a tool-backed action (switch
  workspace, open an app, focus a window, toggle a device, set a timer) and
  the reply CLAIMS it is done ("Done — you're on workspace 4 now") with no
  tool call in the turn. Same single re-run, different nudge; the false
  reply is discarded.

All matchers are pure functions over strings (no network, no state) so
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


# ── claimed-action guard ────────────────────────────────────────────────────
#
# The promised-action guard above catches FUTURE-tense announcements. This
# one catches the past-tense lie: the owner says "Try to switch to 4." and
# the reply answers "Done — you're on workspace 4 now" with NO tool call in
# the turn (the tools log shows none). Two conservative matchers pair up:
#
# * ``detect_action_request`` — is the USER message an imperative action
#   request of the tool-backed kinds (switch workspace/desktop N, open /
#   launch <app>, focus / bring up <window>, turn on/off <device>, set a
#   timer / remind me)? Questions ABOUT such an action ("what workspace am
#   I on?", "did you open Firefox?") and past-tense mentions never match.
#
# * ``claims_completed_action`` — does the final reply CLAIM the action
#   happened ("done", "switched to workspace", "opened", "it's up",
#   "turned on", "timer set", …)?
#
# When BOTH match on a zero-tool-call turn (tools were offered), the server
# re-runs the model once with CLAIM_NUDGE; the false reply is discarded
# (never stored, never spoken). Shares the one-retry-per-turn cap with the
# promised-action guard.

# Request context: the ask must lead the message (bare imperative, with the
# softeners an owner actually types) or carry an explicit modal/polite ask.
_GATE_VERBS = (r"(?:switch|change|move|go|jump|shift|flick|open|launch"
               r"|start|fire|focus|raise|unminimize|bring|turn|toggle|put"
               r"|set|make|create|remind|add)")
_GATE_A = re.compile(
    r"^\s*(?:(?:so|then|now|please|pls|just|quickly|try\s+to"
    r"|go\s+ahead\s+and|could\s+you\s+please|could\s+you|can\s+you\s+please"
    r"|can\s+you|would\s+you|will\s+you)\s+)?" + _GATE_VERBS + r"\b",
    re.IGNORECASE)
_GATE_B = re.compile(
    r"\b(?:can|could|will|would)\s+you\s+(?:please\s+|just\s+|also\s+|simply\s+)?"
    + _GATE_VERBS + r"\b", re.IGNORECASE)
_GATE_C = re.compile(
    r"\bi\s+(?:want|need|would\s+like|ask)\s+you\s+to\s+(?:please\s+|just\s+)?"
    + _GATE_VERBS + r"\b", re.IGNORECASE)

# Never an action request: a question ABOUT the action (interrogatives,
# aspect auxiliaries, plain "to be") anywhere in the message, or a past
# mention of it. Conservative direction: misses cost nothing, false alarms
# cost a wasted retry.
_REQUEST_NEGATIVES = re.compile(
    r"\b(?:what|which|when|why|who|where|how|whose|whether"
    r"|did|does|do|have|has|had|been|was|were|am|is|are"
    r"|yesterday|earlier|already|myself|forgot|used\s+to)\b",
    re.IGNORECASE)

# The tool-backed action kinds (docs/DESKTOP_SENSE.md, docs/TIMERS.md).
# Each pair (verb-context, object) must both match the message.
_ACTION_CASES = [
    (r"\b(?:switch(?:es)?|chang\w*|move\w*|go|jump\w*|shift\w*|flick\w*)\b"
     r"[^.!?\n]{0,20}\b(?:to|onto|over)\b",
     r"\b(?:workspace|desktop|virtual\s+desktop|ws)\s*#?\d{1,2}\b"),
    # bare "switch to 4" / "go to 3" — the number IS the workspace
    (r"\b(?:switch(?:es)?|move\w*|go|jump\w*|flick\w*|chang\w*)\b",
     r"\b(?:to|onto)\s+(?:number\s+|#|workspace\s+|desktop\s+)?\d{1,2}\b"),
    # named workspace/desktop without a number: "switch to the other desktop"
    (r"\b(?:switch(?:es)?|move\w*|go|jump\w*|chang\w*)\b"
     r"[^.!?\n]{0,16}\b(?:to|onto)\b",
     r"\b(?:workspace|desktop|virtual\s+desktop|ws)\b"),
    # open / launch <app>
    (r"\b(?:open|opens|launch\w*|fire\s+up)\b(?:\s+up)?\s+(?:the\s+|my\s+|a\s+"
     r"|an\s+|some\s+)?[a-z]\S*", None),
    # focus / raise / bring up <window>
    (r"\b(?:focus|raise|unminimize)\b\s+(?:on\s+)?\S", None),
    (r"\bbring\s+(?:up|back|forward)\b", None),
    # "switch to the terminal / the previous window / Firefox"
    (r"\b(?:switch|move)\s+(?:over\s+|back\s+)?(?:to|onto)\s+(?:the\s+|my\s+)?"
     r"(?:previous|last|next|other|first|second|third|window|tab)\b", None),
    (r"\b(?:switch|move)\s+(?:over\s+|back\s+)?(?:to|onto)\s+(?:the\s+|my\s+)?"
     r"(?:firefox|chromium|chrome|browser|terminal|files|thunar|spotify"
     r"|steam|discord|godot|blender|code|vscode|obs|krita)\b", None),
    # turn / switch / toggle a device on or off
    (r"\b(?:turn|switch|flip|toggle|put|leave)\b[^.!?\n]{0,20}\b(?:on|off)\b",
     None),
    (r"\btoggle\b\s+\S", None),
    # timers / reminders
    (r"\b(?:set|make|create|start|put|add|give)\b[^.!?\n]{0,20}"
     r"\b(?:timer|reminder|alarm)s?\b", None),
    (r"\bremind\s+me\b", None),
]
_ACTION_CASES = [(re.compile(a, re.IGNORECASE),
                  re.compile(b, re.IGNORECASE) if b else None)
                 for a, b in _ACTION_CASES]


def detect_action_request(text: str) -> bool:
    """True when the USER message imperatively asks for a tool-backed action
    (switch workspace/desktop N, open/launch an app, focus/bring up a
    window, turn a device on/off, set a timer / remind me). Questions about
    such an action and past-tense mentions never match."""
    raw = str(text or "").strip()
    if not raw or _REQUEST_NEGATIVES.search(raw):
        return False
    if not (_GATE_A.search(raw) or _GATE_B.search(raw) or _GATE_C.search(raw)):
        return False
    for verb, obj in _ACTION_CASES:
        if verb.search(raw) and (obj is None or obj.search(raw)):
            return True
    return False


_CLAIM_RES = [re.compile(p, re.IGNORECASE) for p in [
    r"\bdone\b",
    r"\ball\s+set\b",
    r"\bthere\s+you\s+go\b",
    # "I've switched / I just opened / I turned it on"
    r"\bi(?:'|\u2019)?(?:ve|\b(?:have|had|just))?\s+(?:just\s+|now\s+)?"
    r"(?:switched|changed|opened|launched|started|focused|moved|turned"
    r"|toggled|brought|set|created|closed)\b",
    # "switched/moved to workspace 4", "switched over"
    r"\b(?:switched|moved)\b[^.!?\n]{0,24}\b(?:to|over|workspace|desktop)\b",
    # "you're on workspace 4 (now)", "you are now on desktop 2"
    r"\byou(?:'re|\u2019re|\s+are)\b[^.!?\n]{0,16}\b(?:workspace|desktop)\b",
    r"\b(?:now|back)\s+on\b[^.!?\n]{0,16}\b(?:workspace|desktop)\b",
    # "opened Firefox for you", "launched it", "fired it up"
    r"\b(?:opened|launched)\b",
    r"\bfired\b[^.!?\n]{0,12}\bup\b",
    # "it's up", "it is running", "that's open", "the TV is off"
    r"\bit(?:'s|\u2019s|\s+is)\s+(?:now\s+|all\s+)?(?:up|open|running|on"
    r"|off|ready)\b",
    # "turned on the lights", "switched the fan off"
    r"\b(?:turned|switched|flipped|put)\b[^.!?\n]{0,16}\b(?:on|off)\b",
    # "brought it up", "brought the terminal up"
    r"\bbrought\b[^.!?\n]{0,16}\bup\b",
    # "timer set", "reminder is running", "alarm created"
    r"\b(?:timer|reminder|alarm)\b[^.!?\n]{0,24}"
    r"\b(?:set|running|started|created|going)\b",
    r"\b(?:lights?|heater|kettle|fan|tv|speaker|lamp|air\s*con|ac)\b"
    r"[^.!?\n]{0,16}\b(?:on|off)\b",
]]

CLAIM_NUDGE = ("You claimed to do it but called no tool. Call the right "
               "tool now; never claim an action you didn't perform.")


def claims_completed_action(reply: str) -> bool:
    """True when a final reply CLAIMS an action already happened ('Done —
    you're on workspace 4 now', 'Opened it', 'Timer set'…). Checked by the
    server only when ``detect_action_request`` matched the user message and
    the turn made no tool call while tools were offered."""
    text = str(reply or "")
    return any(p.search(text) for p in _CLAIM_RES)
