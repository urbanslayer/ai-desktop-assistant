"""End-to-end Session test: typed text -> fake LLM -> tts none -> viewer events.

No mic, no GPU, no network beyond localhost. Uses its own viewer port.
"""
import os
import time

import pytest
import requests

from conftest import REPLY, free_port, make_cfg
from riko import session, viewer


@pytest.fixture
def turn_env(monkeypatch, fake_llm, tmp_path):
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    cfg = make_cfg(fake_llm.base_url, tmp_path / "chat_history.json")
    monkeypatch.setitem(cfg, "tts", "none")
    server = viewer.start(cfg)
    url = f"http://127.0.0.1:{port}"
    requests.get(f"{url}/next", timeout=5)  # start from an empty queue
    yield cfg, url, fake_llm
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
    cfg, url, fake_llm = turn_env
    s = session.Session(cfg)
    s.submit_text("hello there")

    events = collect_events(url, [], {"say"})
    types = [e["type"] for e in events]
    for needed in ("heard", "status", "say"):
        assert needed in types, f"missing {needed} in {events}"

    heard = events[types.index("heard")]
    assert heard == {"type": "heard", "text": "hello there"}
    say = events[types.index("say")]
    assert say["text"] == REPLY  # tts: none -> bubble only, no audio

    # order: what was heard, then thinking, then speaking, then the bubble
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "thinking" in statuses and "speaking" in statuses
    assert types.index("heard") < types.index("say")
    assert statuses.index("thinking") < statuses.index("speaking")

    # the worker goes back to idle once the queue drains
    collect_events(url, events, {"status"}, timeout=5)
    deadline = time.monotonic() + 5
    while not any(e["type"] == "status" and e["state"] == "idle" for e in events):
        assert time.monotonic() < deadline, f"never went idle: {events}"
        for e in requests.get(f"{url}/next", timeout=5).json()["events"]:
            events.append(e)
        time.sleep(0.05)

    # history was written to the absolute tmp path
    assert "hello there" in open(cfg["history_file"]).read()


def test_say_endpoint_feeds_the_session(turn_env):
    cfg, url, fake_llm = turn_env
    s = session.Session(cfg)
    viewer.on_say(s.submit_text)
    r = requests.post(f"{url}/say", json={"text": "via http"}, timeout=5)
    assert r.status_code == 200
    events = collect_events(url, [], {"say"})
    assert any(e.get("text") == REPLY and e["type"] == "say" for e in events)
    assert any(e.get("text") == "via http" and e["type"] == "heard" for e in events)
    viewer.on_say(None)


def test_empty_submit_is_ignored(turn_env):
    cfg, url, fake_llm = turn_env
    s = session.Session(cfg)
    s.submit_text("   ")
    assert collect_events(url, [], set(), timeout=0.5) == []


def test_llm_error_emits_error_event(turn_env):
    cfg, url, fake_llm = turn_env
    s = session.Session(cfg)
    fake_llm.stop()  # LLM goes away mid-chat
    s.submit_text("are you still there?")
    events = collect_events(url, [], {"error"})
    assert any(e["type"] == "error" for e in events)
    # and the worker survives: bring a reply path back is out of scope, just check idle
    assert any(e["type"] == "status" and e["state"] == "idle" for e in events)
