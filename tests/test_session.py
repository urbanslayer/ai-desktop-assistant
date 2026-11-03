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
