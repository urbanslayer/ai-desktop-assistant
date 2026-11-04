"""End-to-end Session test: typed text -> fake hub -> tts none -> viewer events.

No mic, no GPU, no network beyond localhost. Uses its own viewer port.
"""
import time

import pytest
import requests

from conftest import CHAT_REPLY, HUB_KEY, free_port, make_cfg
from riko import hub_client, session, viewer


@pytest.fixture
def turn_env(monkeypatch, fake_hub, tmp_path):
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    cfg = make_cfg(fake_hub.url)
    server = viewer.start(cfg)
    hub_client.connect(cfg)
    url = f"http://127.0.0.1:{port}"
    requests.get(f"{url}/next", timeout=5)  # start from an empty queue
    yield cfg, url, fake_hub
    server.shutdown()
    server.server_close()


def collect_events(url, got, want_types, timeout=20):
    """Poll /next, appending to `got`, until every wanted event type has arrived."""
    deadline = time.monotonic() + timeout
    seen = {e["type"] for e in got}
    while time.monotonic() < deadline and not want_types <= seen:
        for e in requests.get(f"{url}/next", timeout=5).json()["events"]:
            got.append(e)
            seen.add(e["type"])
        time.sleep(0.05)
    return got


def test_submit_text_end_to_end(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="terminal")
    s.submit_text("hello there")

    events = collect_events(url, [], {"say"})
    types = [e["type"] for e in events]
    for needed in ("heard", "status", "say"):
        assert needed in types, f"missing {needed} in {events}"

    heard = events[types.index("heard")]
    assert heard == {"type": "heard", "text": "hello there"}
    say = events[types.index("say")]
    assert say["text"] == CHAT_REPLY.format(name="Riko", text="hello there")

    # the turn went to the hub stamped with this session's source
    assert fake_hub.chat_requests[-1] == {"text": "hello there", "source": "terminal"}

    # order: what was heard, then thinking, then speaking, then the bubble
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "thinking" in statuses and "speaking" in statuses
    assert types.index("heard") < types.index("say")
    assert statuses.index("thinking") < statuses.index("speaking")

    # the worker goes back to idle once the queue drains
    deadline = time.monotonic() + 5
    while not any(e["type"] == "status" and e["state"] == "idle" for e in events):
        assert time.monotonic() < deadline, f"never went idle: {events}"
        for e in requests.get(f"{url}/next", timeout=5).json()["events"]:
            events.append(e)
        time.sleep(0.05)

    # the hub stored the turn (history lives there now, not in a file)
    history = requests.get(f"{fake_hub.url}/api/history", timeout=5,
                           headers={"Authorization": f"Bearer {HUB_KEY}"}).json()["messages"]
    assert [m["content"] for m in history] == ["hello there",
                                               CHAT_REPLY.format(name="Riko", text="hello there")]
    assert history[0]["source"] == "terminal"


def test_hub_emotion_reaches_the_say_event(turn_env):
    cfg, url, fake_hub = turn_env
    fake_hub.emotion = "happy"
    s = session.Session(cfg, source="terminal")
    s.submit_text("good news")
    events = collect_events(url, [], {"say"})
    say = next(e for e in events if e["type"] == "say")
    assert say["emotion"] == "happy"


def test_say_event_defaults_to_neutral_for_hubs_without_emotion(turn_env):
    cfg, url, fake_hub = turn_env  # fake_hub.emotion is None: old-hub reply shape
    s = session.Session(cfg, source="terminal")
    s.submit_text("hello")
    events = collect_events(url, [], {"say"})
    say = next(e for e in events if e["type"] == "say")
    assert say["emotion"] == "neutral"


def test_switch_greeting_says_happy(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")
    s.switch_persona("asuka")
    events = collect_events(url, [], {"say"})
    say = next(e for e in events if e["type"] == "say")
    assert say["emotion"] == "happy", "greetings should light her face up"


def test_keyword_turn_swaps_persona_and_emits_event(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")
    s.submit_text("switch to asuka")

    events = collect_events(url, [], {"persona", "say"})
    types = [e["type"] for e in events]
    assert "persona" in types, f"no persona event in {events}"
    persona_ev = events[types.index("persona")]
    assert persona_ev["id"] == "asuka" and persona_ev["name"] == "Asuka"
    assert persona_ev["avatar_version"] == 1
    # avatar swaps before the greeting bubble, so she is wearing the new face when she talks
    assert types.index("persona") < types.index("say")
    say = events[types.index("say")]
    assert say["text"] == "Asuka here, senpai."  # the greeting, spoken as the reply
    assert hub_client.active_persona()["id"] == "asuka"

    # and later turns go to the new persona
    requests.get(f"{url}/next", timeout=5)  # drain before checking the next turn
    s.submit_text("hi again")
    events = collect_events(url, [], {"say"}, timeout=5)
    assert CHAT_REPLY.format(name="Asuka", text="hi again") in [e.get("text") for e in events
                                                                if e["type"] == "say"]


def test_switch_persona_call_emits_and_greets(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")
    s.switch_persona("asuka")  # what riko-ctl / the tray do

    events = collect_events(url, [], {"persona", "say"})
    types = [e["type"] for e in events]
    assert "persona" in types and events[types.index("say")]["text"] == "Asuka here, senpai."

    with pytest.raises(hub_client.HubError):
        s.switch_persona("nobody")  # callers see the 404


def test_persona_next_cycles_and_wraps(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")  # fake hub order: riko, asuka

    active = s.persona_next()  # riko -> asuka, announced like any other switch
    assert active["id"] == "asuka"
    assert hub_client.active_persona()["id"] == "asuka"
    events = collect_events(url, [], {"persona", "say"})
    types = [e["type"] for e in events]
    assert events[types.index("persona")]["id"] == "asuka"
    assert events[types.index("say")]["text"] == "Asuka here, senpai."

    requests.get(f"{url}/next", timeout=5)  # drain before the next cycle
    active = s.persona_next()  # wraps around to the first persona
    assert active["id"] == "riko"
    assert hub_client.active_persona()["id"] == "riko"
    assert fake_hub.active == "riko"
    events = collect_events(url, [], {"persona"}, timeout=5)
    assert any(e["type"] == "persona" and e["id"] == "riko" for e in events)


def test_state_poller_notices_an_external_switch(turn_env, monkeypatch):
    cfg, url, fake_hub = turn_env
    session.Session(cfg, source="desktop")
    hub_client.start_state_poller(interval=0.1)

    fake_hub.force_active("asuka")  # switched from the web page / another PC
    events = collect_events(url, [], {"persona"})
    persona_ev = next(e for e in events if e["type"] == "persona")
    assert persona_ev["id"] == "asuka"

    # a hub that briefly disappears is logged once, not spammed, and recovers
    fake_hub.stop()
    time.sleep(0.5)
    drain = requests.get(f"{url}/next", timeout=5).json()["events"]
    assert not any(e["type"] == "persona" for e in drain)  # no event storms while down


def test_forget_clears_history_on_the_hub(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="terminal")
    s.submit_text("remember this")
    collect_events(url, [], {"say"})
    s.forget()
    events = collect_events(url, [], {"say"})
    assert any(e["type"] == "say" and "Who are you again" in e["text"] for e in events)
    history = requests.get(f"{fake_hub.url}/api/history", timeout=5,
                           headers={"Authorization": f"Bearer {HUB_KEY}"}).json()["messages"]
    assert history == []


def test_say_endpoint_feeds_the_session(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="terminal")
    viewer.on_say(s.submit_text)
    r = requests.post(f"{url}/say", json={"text": "via http"}, timeout=5)
    assert r.status_code == 200
    events = collect_events(url, [], {"say"})
    assert any(e.get("text") == CHAT_REPLY.format(name="Riko", text="via http")
               and e["type"] == "say" for e in events)
    assert any(e.get("text") == "via http" and e["type"] == "heard" for e in events)
    viewer.on_say(None)


def test_empty_submit_is_ignored(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="terminal")
    s.submit_text("   ")
    assert collect_events(url, [], set(), timeout=0.5) == []


def test_hub_error_emits_error_event(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="terminal")
    fake_hub.stop()  # the hub goes away mid-chat
    s.submit_text("are you still there?")
    events = collect_events(url, [], {"error"})
    assert any(e["type"] == "error" for e in events)
    # and the worker survives: the turn ends with the idle status
    assert any(e["type"] == "status" and e["state"] == "idle" for e in events)


# ── app-start greeting (owner request 2026-11: "when she loads, she should
#    say her greeting as well") ─────────────────────────────────────────────
def test_greet_on_start_speaks_the_persona_greeting_once(turn_env):
    cfg, url, fake_hub = turn_env
    cfg["tts"] = "hub"  # her greeting voice is the hub's greeting audio
    s = session.Session(cfg, source="desktop")
    s.greet_on_start()  # what desktop.py / main.py do at startup

    events = collect_events(url, [], {"say"})
    say = next(e for e in events if e["type"] == "say")
    assert say["text"] == "Riko here. Miss me, senpai?"
    assert say["emotion"] == "happy", "the app-start hello should light her face up"
    # the voice clip rode along with the say event; tell the page-side player
    # it ended so the worker's viewer.play does not sit on its timeout
    requests.post(f"{url}/done", timeout=5)

    # fetched the greeting audio from the hub exactly once, versioned
    greeting_audio = [p for p in fake_hub.audio_requests if "greeting/audio" in str(p)]
    assert greeting_audio == ["/api/personas/riko/greeting/audio?v=1"]

    # the hello is NOT chat: the hub history stays empty
    history = requests.get(f"{fake_hub.url}/api/history", timeout=5,
                           headers={"Authorization": f"Bearer {HUB_KEY}"}).json()["messages"]
    assert history == []

    # and a second call never re-greets (reconnects / repeated startup calls)
    s.greet_on_start()

    # a state poller re-announcing the SAME persona must not greet again
    hub_client.start_state_poller(interval=0.1)
    time.sleep(1.0)
    drain = []
    for _ in range(6):
        drain += requests.get(f"{url}/next", timeout=5).json()["events"]
        time.sleep(0.05)
    assert not [e for e in drain if e["type"] == "say"], f"poller re-greeted: {drain}"
    assert [p for p in fake_hub.audio_requests if "greeting/audio" in str(p)] == greeting_audio


def test_greet_on_start_can_be_switched_off(turn_env):
    cfg, url, fake_hub = turn_env
    cfg["tts"] = "hub"
    cfg["desktop"]["greet_on_start"] = False
    s = session.Session(cfg, source="desktop")
    s.greet_on_start()
    time.sleep(0.5)  # the page has been polling /next since the fixture
    drain = []
    for _ in range(6):
        drain += requests.get(f"{url}/next", timeout=5).json()["events"]
        time.sleep(0.05)
    assert not [e for e in drain if e["type"] == "say"], f"she greeted despite greet_on_start: false -> {drain}"
    assert fake_hub.audio_requests == []  # no voice work at all


# ── reply body actions (docs/BODY.md §5) ─────────────────────────────────

def test_reply_actions_reach_the_say_event(turn_env):
    cfg, url, fake_hub = turn_env
    fake_hub.actions = [{"name": "wave", "at": 0.0}, {"name": "think", "at": 0.6}]
    s = session.Session(cfg, source="desktop")
    s.submit_text("wave at me")
    say = next(e for e in collect_events(url, [], {"say"}) if e["type"] == "say")
    assert say["actions"] == [{"name": "wave", "at": 0.0}, {"name": "think", "at": 0.6}]


def test_say_actions_empty_for_hubs_without_actions(turn_env):
    cfg, url, fake_hub = turn_env  # fake_hub.actions is None: old-hub reply shape
    s = session.Session(cfg, source="desktop")
    s.submit_text("hello")
    say = next(e for e in collect_events(url, [], {"say"}) if e["type"] == "say")
    assert say["actions"] == []


def test_switch_greeting_waves_when_the_body_can_wave(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")
    s.switch_persona("asuka")  # switch greetings earn a wave (docs/BODY.md §3)
    say = next(e for e in collect_events(url, [], {"say"}) if e["type"] == "say")
    assert say["actions"] == [{"name": "wave", "at": 0}]


def test_switch_greeting_without_wave_in_the_body(turn_env, monkeypatch):
    from riko import client_tools
    monkeypatch.setattr(client_tools, "_current_body",
                        {"actions": ["spin"], "expressions": []})  # e.g. a body set without wave
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")
    s.switch_persona("asuka")
    say = next(e for e in collect_events(url, [], {"say"}) if e["type"] == "say")
    assert say["actions"] == []
