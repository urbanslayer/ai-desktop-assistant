"""Song ticker plumbing on the desktop side (docs/SPOTIFY_NOTES.md).

The desktop session is the ticker's courier: the hub's `now_playing` event
(events long-poll) and the late-client seed become `music:state {json}`
commands on the page queue — never blocking Qt (the follower thread emits
into the viewer queue). The tray checkbox persists through appstate like
the other toggles. Hub-side behaviour lives in tests/test_hub_spotify.py;
the page-side painting policy in client/js/ticker.test.js.
"""
import json
import time

import pytest
import requests

from conftest import free_port, make_cfg
from riko import appstate, hub_client, session, viewer


@pytest.fixture
def ticker_env(monkeypatch, fake_hub, tmp_path):
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    cfg = make_cfg(fake_hub.url)
    server = viewer.start(cfg)
    hub_client.connect(cfg)
    hub_client.note_active(fake_hub.state())
    url = f"http://127.0.0.1:{port}"
    requests.get(f"{url}/next", timeout=5)   # drain the opening status
    yield cfg, url, fake_hub
    server.shutdown()
    server.server_close()


def next_commands(url, timeout=5.0):
    """Poll the page queue until at least one command event shows up."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        evs = requests.get(f"{url}/next", timeout=5).json().get("events") or []
        seen += [e for e in evs if e.get("type") == "command"]
        if seen:
            return seen
    return seen


SNAP = {"playing": True, "uri": "spotify:track:abc", "track": "Curtsey Song",
        "artists": "Riko", "album": "Home", "duration_ms": 200000,
        "position_ms": 1500, "device": "Riko", "context_uri": "spotify:album:x"}


def test_now_playing_event_forwarded_as_command(ticker_env):
    cfg, url, fake_hub = ticker_env
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        fake_hub.push_event("now_playing", **SNAP)
        cmds = next_commands(url)
        music = [c for c in cmds if c["cmd"].startswith("music:state ")]
        assert music, f"no music:state among {cmds}"
        payload = json.loads(music[-1]["cmd"][len("music:state "):])
        assert payload == {k: SNAP[k] for k in (
            "playing", "uri", "track", "artists", "album",
            "duration_ms", "position_ms", "device", "context_uri")}
        # envelope keys (seq/time/type) stay on the hub side
        assert "seq" not in payload and "persona_id" not in payload
    finally:
        s.close()


def test_idle_snapshot_forwarded_so_the_page_hides(ticker_env):
    """The hub publishes idle as playing:false/uri:'' — the page needs that
    exact transition to hide the ticker, so it must NOT be filtered."""
    cfg, url, fake_hub = ticker_env
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        fake_hub.push_event("now_playing", playing=False, uri="", track="",
                            artists="", album="", duration_ms=0,
                            position_ms=0, device="", context_uri="")
        cmds = next_commands(url)
        music = [json.loads(c["cmd"][len("music:state "):])
                 for c in cmds if c["cmd"].startswith("music:state ")]
        assert music and music[-1]["uri"] == "" and music[-1]["playing"] is False
    finally:
        s.close()


def test_late_client_seed_asks_hub_once(ticker_env, monkeypatch):
    """follow_hub_events seeds the ticker from GET /api/spotify/now-playing
    so a page opened mid-song shows the bar without waiting for a change."""
    cfg, url, fake_hub = ticker_env
    calls = []

    def fake_np(self):
        calls.append(1)
        return dict(SNAP)
    monkeypatch.setattr(hub_client.HubClient, "spotify_now_playing", fake_np)
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        cmds = next_commands(url)
        music = [c for c in cmds if c["cmd"].startswith("music:state ")]
        assert music, f"no seed command among {cmds}"
        assert json.loads(music[-1]["cmd"][len("music:state "):])["track"] == "Curtsey Song"
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not calls:
            time.sleep(0.02)
        assert calls == [1]      # exactly one seed call
    finally:
        s.close()


def test_seed_silent_when_hub_says_no(ticker_env, monkeypatch):
    """409 (not connected) or any hub hiccup: no command, no crash."""
    cfg, url, fake_hub = ticker_env

    def boom(self):
        raise hub_client.HubError("spotify not connected", status=409)
    monkeypatch.setattr(hub_client.HubClient, "spotify_now_playing", boom)
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        deadline = time.monotonic() + 0.6
        got = []
        while time.monotonic() < deadline:
            got += requests.get(f"{url}/next", timeout=5).json().get("events") or []
            time.sleep(0.05)
        assert not [e for e in got
                    if str(e.get("cmd", "")).startswith("music:")]
    finally:
        s.close()


# ── the tray toggle's memory (docs/SPOTIFY_NOTES.md) ───────────────────────

def test_music_ticker_appstate_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "st"))
    assert appstate.load_music_ticker() is None          # never chosen
    assert appstate.save_music_ticker(False) is True
    assert appstate.load_music_ticker() is False         # persists across start
    assert appstate.save_music_ticker(True) is True
    assert appstate.load_music_ticker() is True
    assert appstate.save_music_ticker("yes") is False    # non-bools ignored
    assert appstate.load_music_ticker() is True
