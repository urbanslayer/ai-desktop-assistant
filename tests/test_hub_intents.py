"""Intent round: the weather pre-fetch + the promised-action guard.

hub/intents.py decides; hub/server.py acts. The fake Open-Meteo transport
and the scripted LLM come from the weather/tools test files — no network,
no real model. See docs/API.md "Weather pre-fetch" / "Promised-action
guard".
"""
import datetime
import json

import pytest
from fastapi.testclient import TestClient

from hub import intents
from hub.server import create_app
from hub.tools import weather
from test_hub_tools import ScriptedLLM, call_step
from test_hub_weather import FakeOpenMeteo, SAVED_LONDON

# Monday 2026-11-02: "this weekend" needs 7 days, tomorrow is Wed the 3rd.
MONDAY = datetime.date(2026, 11, 2)

# ── matcher table (the contract) ────────────────────────────────────────────
# (text, expected place | None, expected days, expected hourly) — matched
MATCH_CASES = [
    ("What's the weather today?", None, 1, False),
    ("what is the weather", None, 1, False),
    ("How's the weather?", None, 1, False),
    ("What's the weather in Paris?", "Paris", 1, False),
    ("what's the weather like in New York right now", "New York", 1, True),
    ("weather in Tokyo tomorrow?", "Tokyo", 2, False),
    ("Will it rain tomorrow?", None, 2, False),
    ("is it going to rain this weekend?", None, 7, False),
    ("Do I need an umbrella today?", None, 1, False),
    ("should I bring a coat tonight?", None, 1, True),
    ("How hot is it outside?", None, 1, False),
    ("how cold will it be tomorrow", None, 2, False),
    ("What's it like outside?", None, 1, False),
    ("Is it raining?", None, 1, False),
    ("is it sunny outside", None, 1, False),
    ("is it cold today?", None, 1, False),
    ("check the weather", None, 1, False),
    ("tell me the weather in Berlin", "Berlin", 1, False),
    ("what's the forecast for this week?", None, 7, False),
    ("what's the temperature right now?", None, 1, True),
    ("how's the forecast tonight", None, 1, True),
    ("what's it like out there", None, 1, False),
    ("do I need a jacket this afternoon", None, 1, True),
    ("will it snow tomorrow morning", None, 2, False),
    ("weather tomorrow?", None, 2, False),
    ("what's the weekend forecast", None, 7, False),
    ("any chance of rain today?", None, 1, False),
    ("do you think it will rain later?", None, 1, True),
    ("is it going to freeze tonight?", None, 1, True),
    ("need an umbrella?", None, 1, False),
    ("will there be rain tomorrow?", None, 2, False),
    ("how warm is it today?", None, 1, False),
    ("should i wear a coat tomorrow?", None, 2, False),
    ("what's the weather forecast for tomorrow?", None, 2, False),
    ("did you check the weather?", None, 1, False),
    ("I want to know the weather tomorrow", None, 2, False),
    ("what's the weather in Glasgow, UK?", "Glasgow, UK", 1, False),
    ("check if it's raining", None, 1, False),
    ("how is the weather in the evening tomorrow", None, 2, False),
    ("look up the weather", None, 1, False),
    ("do I need boots this weekend?", None, 7, False),
    ("what's the weather for the next few days", None, 3, False),
    ("is it windy", None, 1, False),
    ("I'm curious how cold is it in Reykjavik", "Reykjavik", 1, False),
    ("give me the weather for tomorrow and this week", None, 7, False),
]

# never a weather question
NO_MATCH_CASES = [
    "I'm feeling under the weather",
    "we need to weather the storm",
    "the weather was beautiful yesterday",
    "what was the weather yesterday",
    "how was the weather last night",
    "does the weather tool work?",
    "can you use the weather tool?",
    "what weather apps do you recommend",
    "set a timer to check the weather",
    "remind me to bring an umbrella",
    "do you like talking about the weather",
    "I love this weather",
    "people talk about the weather in England all the time",
    "rain on me parade",
    "can you check the timer",
    "what time is it",
    "is it cold in here",
    "did it rain last night",
    "my umbrella is broken",
    "how do I waterproof my boots",
    "I need a new coat",
    "what's your favorite weather",
    "how are you",
    "weatherproof the deck",
    "check the oven timer",
    "will you marry me",
    "the forecast says rain but I don't believe it",
    "how cold was it yesterday",
    "was it cold yesterday",
    "what's the temperature of the oven",
    "do you think rain water harvesting is worth it",
    "is it too cold for the plants",
    "tomorrow's weather forecast accuracy is famously low",
    "weather",
    "hello",
    "tell me a joke",
    "what do you think about cats?",
    "wave at me",
    "the weather in June is usually rainy",
    "open the godot docs",
]


def test_matcher_table_size():
    assert len(MATCH_CASES) + len(NO_MATCH_CASES) >= 40


@pytest.mark.parametrize("text,place,days,hourly", MATCH_CASES)
def test_weather_questions_match(text, place, days, hourly):
    got = intents.detect_weather_request(text, today=MONDAY)
    assert got is not None, "should match"
    assert got["place"] == place
    assert got["days"] == days and got["hourly"] == hourly


@pytest.mark.parametrize("text", NO_MATCH_CASES)
def test_non_weather_messages_never_match(text):
    assert intents.detect_weather_request(text, today=MONDAY) is None


def test_weekend_scope_is_today_aware():
    ask = intents.detect_weather_request("do I need boots this weekend?",
                                         today=datetime.date(2026, 11, 7))
    assert ask["days"] == 2          # Saturday: Sat+Sun only


PROMISE_YES = [
    "Let me check the weather for you, senpai.",
    "I'll check that now.",
    "One moment, senpai!",
    "Give me a second!",
    "Hold on, I'll look that up.",
    "Just a moment — checking it now.",
    "Hang on, fetching the data.",
    "I'll set a timer for ten minutes.",
    "Let me look that up for you.",
    "I will search the web for it.",
    "I'm going to find out.",
    "Searching for it now, senpai.",
    "I'll pull up the page.",
    "Let me find the answer.",
    "I'll check on that.",
    "Checking that now!",
]
PROMISE_NO = [
    "It is 14 C and light rain in London, senpai.",
    "Nice to meet you, senpai!",
    "You said: hi",
    "I checked — it is sunny today.",
    "I would love that!",
    "See you tomorrow, senpai!",
    "I see, that makes sense.",
    "Twenty seconds, then I'll nudge you, senpai!",
    "Ill-tempered as always!",
    "No, senpai — I said I would and then just stood there like an NPC.",
    "The time is 15:30 in London.",
    "What do you mean, senpai?",
    "I think rain is nice, actually.",
    "Okay! Wave at you [wave]",
    "I'll see you later, senpai!",
    "That made me ill.",
]


@pytest.mark.parametrize("reply", PROMISE_YES)
def test_promises_action_hits(reply):
    assert intents.promises_action(reply)


@pytest.mark.parametrize("reply", PROMISE_NO)
def test_promises_action_spares(reply):
    assert not intents.promises_action(reply)


# ── the pre-fetch through /api/chat ─────────────────────────────────────────

@pytest.fixture
def wx():
    fake = FakeOpenMeteo()
    weather.TRANSPORT = fake
    weather.clear_cache()
    yield fake
    weather.TRANSPORT = None
    weather.clear_cache()


def make_client(tmp_path, steps, tools_defaults=None):
    llm = ScriptedLLM(steps)
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     tools_defaults=tools_defaults or {})
    client = TestClient(app)
    return client, llm


def save_london(client):
    assert client.put("/api/settings", json={"location": SAVED_LONDON}).status_code == 200


def test_prefetch_injects_data_one_round_trip_and_records_auto(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "Light rain, 14 C, senpai."}])
    save_london(client)
    res = client.post("/api/chat", json={"text": "What's the weather today?"})
    assert res.status_code == 200
    body = res.json()
    assert body["reply"] == "Light rain, 14 C, senpai."
    # ONE model call: the data was already in the prompt
    assert len(llm.chat_requests) == 1
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert "Live weather data (fetched just now for London" in sysmsg
    assert "Answer from this; do not call the weather tool again" in sysmsg
    assert "temp_c" in sysmsg and "14.2" in sysmsg
    # chip + stored trace: labelled, hub source, auto
    assert body["tools_used"] == [
        {"tool": "weather", "label": "checking the weather in London",
         "source": "hub", "auto": True}]
    # tool log + events
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["tool"] == "weather" and log[0]["ok"] is True
    assert log[0]["source"] == "hub"
    assert log[0]["approval"] == "auto (hub pre-fetch)"
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    started = [e for e in evs if e["type"] == "tool_started"]
    assert any(e["tool"] == "weather"
               and e["label"] == "checking the weather in London"
               for e in started)


def test_prefetch_explicit_place_geocodes(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "Sunny in Tokyo!"}])
    save_london(client)
    res = client.post("/api/chat", json={"text": "What's the weather in Tokyo?"})
    assert res.json()["tools_used"][0]["label"] == "checking the weather in Tokyo"
    assert any(c["params"]["name"] == "Tokyo" for c in wx.geocode_calls())
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert "fetched just now for Tokyo (Tokyo, Japan)" in sysmsg


def test_prefetch_tomorrow_uses_two_days(tmp_path, wx):
    client, _ = make_client(tmp_path, [{"content": "78% chance, senpai."}])
    save_london(client)
    client.post("/api/chat", json={"text": "will it rain tomorrow?"})
    call = wx.forecast_calls()[-1]
    assert call["params"]["forecast_days"] == 2
    assert "hourly" not in call["params"]


def test_prefetch_tonight_adds_hourly(tmp_path, wx):
    client, _ = make_client(tmp_path, [{"content": "Bring the coat."}])
    save_london(client)
    client.post("/api/chat", json={"text": "do I need an umbrella tonight?"})
    call = wx.forecast_calls()[-1]
    assert "temperature_2m" in call["params"]["hourly"]


def test_prefetch_failure_falls_back_with_hint(tmp_path, wx):
    wx.fail_geocode = True
    client, llm = make_client(tmp_path, [{"content": "Which place, senpai?"}])
    save_london(client)   # saved place is fine; the NAMED place fails
    res = client.post("/api/chat", json={"text": "what's the weather in Atlantis?"})
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert "Live weather data" not in sysmsg
    assert "call the weather tool" in sysmsg.lower()
    assert res.json()["tools_used"] == []
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["tool"] == "weather" and log[0]["ok"] is False
    assert "geocode down" in log[0]["error"]


def test_prefetch_no_location_hints_tool(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "Where are we?"}])
    res = client.post("/api/chat", json={"text": "what's the weather today?"})
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert "Live weather data" not in sysmsg
    assert "call the weather tool" in sysmsg.lower()
    assert res.json()["tools_used"] == []
    assert wx.calls == []   # nothing to fetch for: no geocoding attempts


def test_prefetch_runs_with_tools_off(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "14 C and raining."}],
                              tools_defaults={"enabled": False})
    save_london(client)
    res = client.post("/api/chat", json={"text": "what's the weather today?"})
    assert "Live weather data (fetched just now for London" in \
        llm.chat_requests[0]["messages"][0]["content"]
    assert "tools" not in llm.chat_requests[0]     # no function calling at all
    assert res.json()["tools_used"] == [
        {"tool": "weather", "label": "checking the weather in London",
         "source": "hub", "auto": True}]


def test_prefetch_follows_units_preset(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "62 F, senpai."}])
    save_london(client)
    assert client.put("/api/settings",
                      json={"tools": {"units": "us"}}).status_code == 200
    client.post("/api/chat", json={"text": "what's the weather today?"})
    assert wx.forecast_calls()[-1]["params"]["temperature_unit"] == "fahrenheit"
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert '"temperature": "\u00b0F"' in sysmsg


def test_permission_never_skips_prefetch(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "ok"}],
                              tools_defaults={"permissions": {"weather": "never"}})
    save_london(client)
    res = client.post("/api/chat", json={"text": "what's the weather today?"})
    assert "Live weather data" not in llm.chat_requests[0]["messages"][0]["content"]
    assert res.json()["tools_used"] == []
    assert wx.calls == []


# ── the promised-action guard through /api/chat ─────────────────────────────

ANNOUNCE = "Let me check the weather for you, senpai."
NON_WEATHER = "can you look up the latest Godot version?"


def test_guard_reruns_once_and_replaces_the_reply(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        {"content": ANNOUNCE},
        {"content": "Godot 4.5 is out, senpai."}])
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    assert res.json()["reply"] == "Godot 4.5 is out, senpai."
    assert len(llm.chat_requests) == 2
    retry_msgs = llm.chat_requests[1]["messages"]
    assert retry_msgs[-2]["role"] == "assistant"
    assert retry_msgs[-2]["content"] == ANNOUNCE
    assert retry_msgs[-1]["role"] == "system"
    assert "didn't call a tool" in retry_msgs[-1]["content"]
    # the announced-only reply is discarded: never stored, not in history
    hist = client.get("/api/history").json()["messages"]
    assert [m["content"] for m in hist if m["role"] == "assistant"] == \
        ["Godot 4.5 is out, senpai."]


def test_guard_retry_that_calls_a_tool_replaces_the_reply(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        {"content": ANNOUNCE},
        call_step("get_time", {}),
        {"content": "It is 15:30 in London."}])
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    assert res.json()["reply"] == "It is 15:30 in London."
    assert res.json()["tools_used"] == [
        {"tool": "get_time", "label": "checking the date and time",
         "args": {}}]
    assert len(llm.chat_requests) == 3


def test_guard_no_retry_when_a_tool_ran(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        call_step("get_time", {}),
        {"content": "One moment!"}])
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    # "One moment!" announced but the turn HAD a tool call: no second chance
    assert res.json()["reply"] == "One moment!"
    assert len(llm.chat_requests) == 2


def test_guard_no_retry_for_plain_replies(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "Nice to meet you, senpai!"}])
    client.post("/api/chat", json={"text": "hello"})
    assert len(llm.chat_requests) == 1


def test_guard_retries_only_once(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        {"content": ANNOUNCE},
        {"content": "I'll look that up!"},
        {"content": "NEVER REACHED"}])
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    # cap: one retry — the second announcement stands, no third call
    assert res.json()["reply"] == "I'll look that up!"
    assert len(llm.chat_requests) == 2


def test_guard_skipped_when_tools_are_off(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": ANNOUNCE}],
                              tools_defaults={"enabled": False})
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    assert res.json()["reply"] == ANNOUNCE   # no tools to call: no retry
    assert len(llm.chat_requests) == 1


def test_guard_skipped_when_server_rejects_tools(tmp_path, wx):
    llm = ScriptedLLM([{"content": ANNOUNCE}], reject_tools=True)
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"})
    client = TestClient(app)
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    # tools were rejected: retrying the model would hit the same wall, so
    # the guard stands down — no nudge message ever reaches the server
    assert res.json()["reply"] == ANNOUNCE
    assert all("didn't call a tool" not in json.dumps(r["messages"])
               for r in llm.chat_requests)


def test_guard_still_honours_emotion_on_the_final_reply(tmp_path, wx):
    L, R = chr(91), chr(93)
    client, llm = make_client(tmp_path, [
        {"content": ANNOUNCE},
        {"content": f"{L}happy{R} 4.5, senpai!"}])
    res = client.post("/api/chat", json={"text": NON_WEATHER})
    assert res.json()["reply"] == "4.5, senpai!"
    assert res.json()["emotion"] == "happy"


# ── claimed-action guard: matchers (the contract) ───────────────────────────
# The live bug: owner "Try to switch to 4." -> Furina "Done — Hyprland
# confirms you're on workspace 4 now" with NO tool call in the turn.

ACTION_REQUEST_HITS = [
    "Try to switch to 4.",
    "switch to workspace 3",
    "Go to desktop 2",
    "move to 7",
    "change to workspace 5",
    "switch to the other desktop",
    "open firefox",
    "Launch Godot",
    "please open a terminal",
    "could you open firefox",
    "just open spotify",
    "go ahead and open firefox",
    "I want you to open Blender",
    "focus the terminal",
    "bring up the editor",
    "switch to the previous window",
    "switch to firefox",
    "turn on the lights",
    "switch the fan off",
    "toggle the lamp",
    "put the heating on",
    "set a timer for 10 minutes",
    "remind me to stretch at 6",
]

# questions ABOUT such an action, past mentions, plain chat: never a request
ACTION_REQUEST_MISSES = [
    "what workspace am I on?",
    "did you switch to workspace 4?",
    "am I on workspace 4?",
    "how do I switch workspaces?",
    "what apps are open?",
    "is firefox open?",
    "have you opened firefox",
    "was I on workspace 2",
    "which workspace has the terminal",
    "where did the window go",
    "can you see my windows?",
    "tell me a joke",
    "I switched to workspace 3 earlier",
    "thanks for opening firefox",
    "what is the weather",
    "hello",
    "what timers do I have",
    "can you check my timers",
    "wave at me",
    "do you like firefox",
    "open",
    "what time is it",
    "check the oven timer",
    "will you marry me",
]

CLAIM_HITS = [
    "Done — Hyprland confirms you're on workspace 4 now",
    "Done!",
    "Switched to workspace 2.",
    "I've opened Firefox for you.",
    "It's up now.",
    "I just launched Godot.",
    "Lights are on — turned them on.",
    "I turned it off.",
    "All set — timer running.",
    "Timer set for 10 minutes.",
    "There you go!",
    "I've switched you over.",
    "You are now on workspace 4.",
    "Brought the terminal up.",
    "Moved to desktop 3.",
    "I created the reminder.",
    "Fired it up!",
    "The TV is off now.",
]

# offers, questions and plain chatter claim nothing
CLAIM_MISSES = [
    "I can switch whenever you like.",
    "Which workspace should I open?",
    "Let me check.",
    "Nice to meet you, senpai!",
    "It was beautiful yesterday.",
    "Opening the app needs a launcher name — which one?",
    "Sure — which app should I open?",
    "What shall I open first?",
    "workspace 4 has your terminal open",
    "I could turn on the heater if you like.",
    "The lights in the photo are lovely.",
]


@pytest.mark.parametrize("text", ACTION_REQUEST_HITS)
def test_detect_action_request_hits(text):
    assert intents.detect_action_request(text)


@pytest.mark.parametrize("text", ACTION_REQUEST_MISSES)
def test_detect_action_request_misses(text):
    assert not intents.detect_action_request(text)


@pytest.mark.parametrize("reply", CLAIM_HITS)
def test_claims_completed_action_hits(reply):
    assert intents.claims_completed_action(reply)


@pytest.mark.parametrize("reply", CLAIM_MISSES)
def test_claims_completed_action_spares(reply):
    assert not intents.claims_completed_action(reply)


# ── the claimed-action guard through /api/chat ──────────────────────────────

OWNER_ASK = "Try to switch to 4."
FALSE_DONE = "Done — Hyprland confirms you're on workspace 4 now."
# the same completion claim WITHOUT citing a tool: the claimed-action guard
# alone is under test here ('Hyprland confirms' now also trips the
# hallucinated-tool-talk twin, tested in tests/test_hub_workspace.py)
NO_CITE = "Done — you're on workspace 4 now."


def test_claimed_action_reruns_once_with_the_claim_nudge(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        {"content": FALSE_DONE},
        call_step("get_time", {}),
        {"content": "On workspace 4 now, senpai."}])
    res = client.post("/api/chat", json={"text": OWNER_ASK})
    assert res.json()["reply"] == "On workspace 4 now, senpai."
    assert res.json()["tools_used"] == [
        {"tool": "get_time", "label": "checking the date and time",
         "args": {}}]
    # the retry carries the CLAIM nudge, not the promise one
    nudge = llm.chat_requests[1]["messages"][-1]
    assert nudge["role"] == "system"
    assert "never claim an action you didn't perform" in nudge["content"]
    assert "You said you would check" not in nudge["content"]
    # the false reply is discarded: never stored, never spoken
    hist = client.get("/api/history").json()["messages"]
    assert [m["content"] for m in hist if m["role"] == "assistant"] == \
        ["On workspace 4 now, senpai."]


def test_claimed_action_question_does_not_rerun(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": NO_CITE}])
    res = client.post("/api/chat", json={"text": "what workspace am I on?"})
    assert res.json()["reply"] == NO_CITE   # a question: no retry
    assert len(llm.chat_requests) == 1


def test_claimed_action_plain_chat_does_not_rerun(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": NO_CITE}])
    res = client.post("/api/chat", json={"text": "hello"})
    assert res.json()["reply"] == NO_CITE   # no action was asked for
    assert len(llm.chat_requests) == 1


def test_claimed_action_reply_that_called_a_tool_does_not_rerun(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        call_step("get_time", {}),
        {"content": FALSE_DONE}])
    res = client.post("/api/chat", json={"text": OWNER_ASK})
    # a tool ran this turn: the claim stands, no second chance
    assert res.json()["reply"] == FALSE_DONE
    assert len(llm.chat_requests) == 2
    assert all("never claim an action" not in json.dumps(r["messages"])
               for r in llm.chat_requests)


def test_claimed_action_retries_only_once(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        {"content": FALSE_DONE},
        {"content": "Still done — you're on workspace 4."},
        {"content": "NEVER REACHED"}])
    res = client.post("/api/chat", json={"text": OWNER_ASK})
    # cap: one retry per turn — the second claim stands, no third call
    assert res.json()["reply"] == "Still done — you're on workspace 4."
    assert len(llm.chat_requests) == 2


def test_claimed_action_skipped_when_tools_are_off(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": FALSE_DONE}],
                              tools_defaults={"enabled": False})
    res = client.post("/api/chat", json={"text": OWNER_ASK})
    assert res.json()["reply"] == FALSE_DONE   # no tools to call: no retry
    assert len(llm.chat_requests) == 1


def test_promise_guard_wins_when_a_reply_matches_both(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        {"content": "Let me check — done, you're on workspace 4!"},
        {"content": "On workspace 4."}])
    res = client.post("/api/chat", json={"text": OWNER_ASK})
    assert res.json()["reply"] == "On workspace 4."
    assert len(llm.chat_requests) == 2   # the shared cap: one retry total
    nudge = llm.chat_requests[1]["messages"][-1]["content"]
    assert "didn't call a tool" in nudge   # the promise nudge, checked first


def test_claimed_timer_claim_retries_then_fallback_still_creates(tmp_path, wx):
    # "set a timer" asked, false claim -> one nudge retry; the retry keeps
    # claiming, so the deterministic timer fallback creates it anyway
    client, llm = make_client(tmp_path, [
        {"content": "Done — timer set, senpai."},
        {"content": "I set it!"}])
    res = client.post("/api/chat", json={"text": "set a timer for 5 minutes"})
    assert len(llm.chat_requests) == 2
    assert res.status_code == 200
    timers = res.json()["timers_set"]
    assert len(timers) == 1 and timers[0]["asked"] is True


def test_tools_prompt_carries_the_confirmation_rule(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "ok"}])
    client.post("/api/chat", json={"text": "hello"})
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert ("Only say you did something after the tool result confirms it"
            in sysmsg)
    assert "if a tool fails, say what failed" in sysmsg
    assert ("Never suggest the owner press keys for you when a tool exists"
            in sysmsg)
