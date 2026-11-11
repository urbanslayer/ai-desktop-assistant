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


# ── fast path: desktop-local Soloist beat → page paint + hub nudge ──────────
# (docs/SPOTIFY_NOTES.md, 2026-11-11 live-bug round)

def collect_music(url, seconds):
    """All music:state payloads seen on the page queue for `seconds`."""
    out, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        evs = requests.get(f"{url}/next", timeout=5).json().get("events") or []
        out += [json.loads(e["cmd"][len("music:state "):])
                for e in evs
                if str(e.get("cmd", "")).startswith("music:state ")]
        time.sleep(0.05)
    return out


def test_music_local_paints_and_nudges(ticker_env, monkeypatch):
    """The Soloist feed's beat: forward at once + nudge the hub's fast poll.
    A hub event that merely CONFIRMS the same state is not repainted (no
    flicker); newer hub state always wins."""
    cfg, url, fake_hub = ticker_env
    nudges = []
    monkeypatch.setattr(hub_client.HubClient, "spotify_wake",
                        lambda self: nudges.append(1))
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        s.music_local(dict(SNAP))
        got = collect_music(url, 0.6)
        assert got and got[-1]["uri"] == SNAP["uri"]
        assert len(nudges) == 1                       # hub fast poll nudged
        # the hub's confirming event (same uri+playing): skipped, no repaint
        fake_hub.push_event("now_playing", **SNAP)
        assert collect_music(url, 1.0) == []
        # newer hub state inside the same window: forwarded, newest wins
        other = dict(SNAP, uri="spotify:track:next", track="Next Song")
        fake_hub.push_event("now_playing", **other)
        got = collect_music(url, 3.0)
        assert got and got[-1]["uri"] == "spotify:track:next"
        assert len(nudges) == 1                       # events never re-nudge
    finally:
        s.close()


def test_music_local_stale_confirmation_forwards(ticker_env, monkeypatch):
    """Outside the reconcile window the hub event is NOT a confirmation:
    it forwards again (the page policy is idempotent, hiding wins)."""
    cfg, url, fake_hub = ticker_env
    monkeypatch.setattr(hub_client.HubClient, "spotify_wake",
                        lambda self: None)
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        s.music_local(dict(SNAP))
        assert collect_music(url, 0.6)
        s._music_fast = (SNAP["uri"], True, time.time() - 60.0)  # window gone
        fake_hub.push_event("now_playing", **SNAP)
        got = collect_music(url, 3.0)
        assert got and got[-1]["uri"] == SNAP["uri"]
    finally:
        s.close()


class _FakeFeedConn:
    """One quiet fake of the Soloist WS seam: auth + one playing event,
    then silence (TimeoutError, like the real sync client)."""

    def __init__(self):
        self.msgs = [{"type": "auth_state", "logged_in": True,
                      "is_active": True, "device_name": "Riko"},
                     {"type": "playback_state", "status": "playing",
                      "item": {"uri": "spotify:track:soloist-x",
                               "entity_type": "track",
                               "decorations": {
                                   "identity": {"name": "Soloist Song"}}}}]

    def send(self, data):
        pass

    def recv(self, timeout=None):
        if self.msgs:
            return json.dumps(self.msgs.pop(0))
        raise TimeoutError

    def close(self):
        pass


class _FakeFeedWs:
    def open(self):
        return _FakeFeedConn()


def _beat_closure(s):
    """The desktop.py closure, verbatim logic."""
    def beat(local):
        try:
            snap = s.hub.spotify_now_playing()
        except Exception:
            snap = local
        s.music_local(snap)
    return beat


def test_feed_beat_closure_reaches_page_end_to_end(ticker_env, monkeypatch):
    """Soloist WS event → desktop beat closure → hub now-playing GET → page
    music:state: painted from the HUB's answer (single source of truth),
    never from the locally-mapped WS snapshot."""
    from riko.music.soloist import SoloistFeed

    cfg, url, fake_hub = ticker_env
    hub2 = dict(SNAP, uri="spotify:track:hub-truth", track="Hub Truth")
    calls = []

    def np(self):
        calls.append(1)
        return dict(SNAP) if len(calls) == 1 else dict(hub2)
    monkeypatch.setattr(hub_client.HubClient, "spotify_wake",
                        lambda self: None)
    monkeypatch.setattr(hub_client.HubClient, "spotify_now_playing", np)
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)          # the seed paints SNAP first
    feed = SoloistFeed(_FakeFeedWs(), _beat_closure(s), log=lambda *a: None,
                       retry_s=0.05, recv_timeout=0.05)
    feed.start()
    try:
        got = collect_music(url, 2.0)
        # beat → hub GET (call 2) → HUB answer painted, not the local map
        assert got and got[-1]["uri"] == "spotify:track:hub-truth"
        assert all(g["uri"] != "spotify:track:soloist-x" for g in got)
    finally:
        feed.stop()
        s.close()


def test_beat_paints_local_snapshot_when_hub_unreachable(ticker_env,
                                                         monkeypatch):
    """Hub down mid-beat: painting the locally-mapped Soloist snapshot is
    better than leaving the page dark (docs/SPOTIFY_NOTES.md)."""
    from riko.music.soloist import SoloistFeed

    cfg, url, fake_hub = ticker_env

    def flaky(self):
        raise hub_client.HubUnreachable("hub is down")
    monkeypatch.setattr(hub_client.HubClient, "spotify_wake",
                        lambda self: None)
    monkeypatch.setattr(hub_client.HubClient, "spotify_now_playing", flaky)
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)          # seed fails silently (hub down)
    feed = SoloistFeed(_FakeFeedWs(), _beat_closure(s), log=lambda *a: None,
                       retry_s=0.05, recv_timeout=0.05)
    feed.start()
    try:
        got = collect_music(url, 2.0)
        assert got and got[-1]["uri"] == "spotify:track:soloist-x"
        assert got[-1]["track"] == "Soloist Song"
    finally:
        feed.stop()
        s.close()
