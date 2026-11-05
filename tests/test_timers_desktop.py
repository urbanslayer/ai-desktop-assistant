"""Desktop timer follow (docs/TIMERS.md D) against the fake hub's events.

The hub fires timers; the desktop follows GET /api/events and:
  - speaks a fired reply for the ACTIVE persona exactly like a normal reply
    (bubble, hub audio_url voice, emotion, actions), queued behind her work;
  - shows a tray line for OTHER personas without switching;
  - reports POST /api/chat/read on window focus, throttled per persona;
  - breaks on nothing when the hub is too old for timers/events.
"""
import time

import pytest
import requests

from conftest import CHAT_REPLY, free_port, make_cfg
from riko import hub_client, session, viewer


@pytest.fixture
def timer_env(monkeypatch, fake_hub, tmp_path):
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


def wait_until(fn, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return False


def start_following(s):
    # short long-poll: the fake hub wakes on push_event anyway
    s.follow_hub_events(wait=1)


def push_timer_reply(fake_hub, persona_id="riko", message_id=99, reply="The kettle, senpai!",
                     label="tea", **extra):
    """Both events a fired timer produces (docs/TIMERS.md H)."""
    fake_hub.push_event("timer_fired", id=7, label=label, persona_id=persona_id,
                        message_id=message_id)
    ev = {"origin": "timer", "persona_id": persona_id, "message_id": message_id,
          "reply": reply, "speech": reply, "emotion": "worried",
          "actions": [{"name": "think", "at": 0.0}],
          "audio_url": f"/api/messages/{message_id}/audio"}
    ev.update(extra)
    fake_hub.push_event("turn_finished", **ev)
    return ev


def test_timer_reply_for_active_persona_is_spoken_with_voice(timer_env):
    cfg, url, fake_hub = timer_env
    cfg["tts"] = "hub"  # her voice is the hub's audio_url, like any other reply
    # the hub stored the fired reply, so its audio route serves it
    fake_hub.messages.append({"id": 99, "persona": "riko", "role": "assistant",
                              "content": "The kettle, senpai!", "source": "timer",
                              "created_at": "2026-11-05T00:00:00Z"})
    s = session.Session(cfg, source="desktop")
    notices = []
    s.on_timer_notice = lambda name, label: notices.append((name, label))
    start_following(s)

    requests.get(f"{url}/next", timeout=5)  # keep the page "open" for viewer.play
    push_timer_reply(fake_hub)
    events = collect_events(url, [], {"say"})
    requests.post(f"{url}/done", timeout=5)  # the clip "ended": free the worker

    say = next(e for e in events if e["type"] == "say")
    assert say["text"] == "The kettle, senpai!"
    assert say["emotion"] == "worried"
    assert say["actions"] == [{"name": "think", "at": 0.0}]
    assert "/api/messages/99/audio" in [str(p) for p in fake_hub.audio_requests]
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "speaking" in statuses
    # active persona -> spoken, never a tray line
    assert notices == []


def test_timer_reply_waits_while_she_is_busy(timer_env):
    cfg, url, fake_hub = timer_env
    cfg["tts"] = "hub"  # her voice blocks the worker until the page says /done
    s = session.Session(cfg, source="desktop")
    start_following(s)

    requests.get(f"{url}/next", timeout=5)
    s.submit_text("long turn")
    events = collect_events(url, [], {"say"})  # she is now mid-speech, holding the worker
    assert events[-1]["text"] == CHAT_REPLY.format(name="Riko", text="long turn")

    push_timer_reply(fake_hub, message_id=199)
    # she keeps talking: the timer reply must sit in the queue, not interrupt
    quiet = []
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        quiet += requests.get(f"{url}/next", timeout=5).json()["events"]
        time.sleep(0.05)
    assert not [e for e in quiet if e["type"] == "say" and "kettle" in str(e.get("text"))]

    requests.post(f"{url}/done", timeout=5)  # her sentence ends
    events = collect_events(url, [], {"say"}, timeout=10)  # now the timer plays
    requests.post(f"{url}/done", timeout=5)
    assert any(e["type"] == "say" and e["text"] == "The kettle, senpai!" for e in events)


def test_other_persona_timer_notifies_the_tray_without_switching(timer_env):
    cfg, url, fake_hub = timer_env
    s = session.Session(cfg, source="desktop")
    notices = []
    s.on_timer_notice = lambda name, label: notices.append((name, label))
    start_following(s)

    push_timer_reply(fake_hub, persona_id="asuka", message_id=299, reply="Groceries!",
                     label="groceries")
    assert wait_until(lambda: notices == [("Asuka", "groceries")]), \
        f"no tray line for the other persona: {notices}"

    # ... and nothing else happens: no bubble, no persona switch
    drain = []
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        drain += requests.get(f"{url}/next", timeout=5).json()["events"]
        time.sleep(0.05)
    assert not [e for e in drain if e["type"] in ("say", "persona")], drain
    assert fake_hub.active == "riko"
    assert (hub_client.active_persona() or {}).get("id") == "riko"


def test_own_turns_and_double_deliveries_are_not_double_played(timer_env):
    cfg, url, fake_hub = timer_env  # tts: none -> the worker never blocks on audio
    s = session.Session(cfg, source="desktop")
    notices = []
    s.on_timer_notice = lambda name, label: notices.append((name, label))
    start_following(s)

    s.submit_text("mine")
    events = collect_events(url, [], {"say"})
    own_say = CHAT_REPLY.format(name="Riko", text="mine")

    # the events of my own turn come back too (no origin): they must not echo
    fake_hub.push_event("turn_finished", persona="riko", message_id=1, emotion="neutral")
    # and a timer whose events the ring delivers TWICE must speak exactly once
    push_timer_reply(fake_hub, message_id=399)
    fake_hub.push_event("turn_finished", origin="timer", persona_id="riko",
                        message_id=399, reply="The kettle, senpai!",
                        audio_url=None, actions=[], emotion="neutral")
    time.sleep(1.0)
    events += requests.get(f"{url}/next", timeout=5).json()["events"]
    says = [e.get("text") for e in events if e["type"] == "say"]
    assert says.count(own_say) == 1, says
    assert says.count("The kettle, senpai!") == 1, says
    assert notices == []  # my persona's own timer is spoken, not tray-noticed


def test_events_buffered_before_start_are_not_replayed(timer_env):
    cfg, url, fake_hub = timer_env
    s = session.Session(cfg, source="desktop")
    # a timer fired (and was already spoken elsewhere) before this app started
    push_timer_reply(fake_hub, message_id=499)
    time.sleep(0.2)
    start_following(s)  # the follower primes its cursor: no replay storm
    s.submit_text("after start")
    events = collect_events(url, [], {"say"})
    time.sleep(0.5)
    events += requests.get(f"{url}/next", timeout=5).json()["events"]
    says = [e.get("text") for e in events if e["type"] == "say"]
    assert says == [CHAT_REPLY.format(name="Riko", text="after start")], says


def test_chat_read_reported_on_see_and_throttled_per_persona(fake_hub, hub_connected):
    assert (hub_client.active_persona() or {}).get("id") == "riko"
    hub_client.note_chat_read()  # window shown/focused for Riko
    assert wait_until(lambda: fake_hub.read_requests == [{"persona_id": "riko"}])

    hub_client.note_chat_read()  # focus again inside the 30 s window
    time.sleep(0.3)
    assert len(fake_hub.read_requests) == 1, fake_hub.read_requests

    hub_client.note_chat_read(persona_id="asuka")  # throttle is per persona
    assert wait_until(lambda: len(fake_hub.read_requests) == 2)
    assert fake_hub.read_requests[1] == {"persona_id": "asuka"}

    hub_client.note_chat_read(throttle_s=0)  # the throttle expires: reported again
    assert wait_until(lambda: len(fake_hub.read_requests) == 3)


def test_old_hub_without_timers_breaks_nothing(timer_env):
    cfg, url, fake_hub = timer_env
    fake_hub.has_events = False      # a hub older than the events route
    fake_hub.has_chat_read = False   # ... and older than chat.read
    s = session.Session(cfg, source="desktop")
    notices = []
    s.on_timer_notice = lambda name, label: notices.append((name, label))
    start_following(s)  # the follower sees 404 and stops quietly

    s.submit_text("still working?")  # ordinary chat is untouched
    events = collect_events(url, [], {"say"})
    assert CHAT_REPLY.format(name="Riko", text="still working?") in \
        [e.get("text") for e in events if e["type"] == "say"]

    hub_client.note_chat_read()  # 404, ignored silently, no raise
    time.sleep(0.3)
    assert fake_hub.read_requests == []
    assert notices == []
    assert not [e for e in events if e["type"] == "error"]
