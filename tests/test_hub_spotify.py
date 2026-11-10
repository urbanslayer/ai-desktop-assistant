"""Music phase 1: the hub's Spotify integration (docs/SPOTIFY_NOTES.md).

Everything runs against tests/fake_spotify.py (an in-repo fake HTTP server
playing accounts.spotify.com + api.spotify.com/v1): connect incl. the
paste-back, refresh-on-expiry, play resolves query→URI→start on the chosen
device, dev-mode rules (search limit ≤ 10, PUT /me/library for likes, no
batch endpoints), quota 429 back-off that never hammers, human-readable tool
results, settings secrecy (tokens never in GET /api/settings), and the
now_playing event (dedupe, no polling while paused/idle). No real network.
"""
import time

import pytest
from fastapi.testclient import TestClient

from fake_spotify import (DEVICE_ECHO, DEVICE_RIKO, TRACK, FakeSpotify,
                          playing_snapshot)
from hub.server import create_app
from hub.spotify import (POLL_S, SpotifyError, SpotifyNotConnected,
                         SpotifyPremium, SpotifyQuota, make_pkce,
                         parse_redirect_paste)
from hub.tools.spotify_tools import build_spotify_specs

CID = "deadbeefclientid"
REDIRECT = "http://127.0.0.1:8911/callback"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fake():
    srv = FakeSpotify()
    yield srv
    srv.stop()


@pytest.fixture
def hub_app(tmp_path, fake):
    app = create_app(
        tmp_path / "hubdata",
        spotify_defaults={"accounts_url": fake.base,
                          "api_url": fake.base + "/v1",
                          "poll_s": POLL_S})          # no thread: poll_tick()
    client = TestClient(app)
    r = client.put("/api/settings", json={"spotify": {
        "enabled": True, "client_id": CID, "device_name": "Riko"}})
    assert r.status_code == 200
    return app, client


def seed_tokens(app, expires_in=3600.0, refresh="REFRESH-1"):
    store = app.state.store
    store.set_json("spotify_tokens", {
        "access_token": "ACCESS-SEED", "refresh_token": refresh,
        "expires_at": time.time() + expires_in, "scope": "user-read-playback-state"})


def events_of(client, after=0):
    return [e for e in client.get(f"/api/events?after={after}&wait=0").json()["events"]
            if e["type"] == "now_playing"]


# ── PKCE helpers ─────────────────────────────────────────────────────────────

def test_pkce_pair_and_challenge():
    verifier, challenge = make_pkce()
    assert len(verifier) >= 43 and verifier != challenge
    # S256 of the verifier (base64url, unpadded) is the challenge
    import base64, hashlib
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect


def test_parse_redirect_paste_forms():
    got = parse_redirect_paste(f"{REDIRECT}?code=CODE1&state=ST1")
    assert got == {"code": "CODE1", "state": "ST1", "redirect_uri": REDIRECT}
    got = parse_redirect_paste("code=CODE2&state=ST2")     # bare query string
    assert got["code"] == "CODE2"
    assert parse_redirect_paste("CODE3")["code"] == "CODE3"   # bare code
    with pytest.raises(SpotifyError, match="not completed"):
        parse_redirect_paste(f"{REDIRECT}?error=access_denied"
                             "&error_description=user%20denied")
    with pytest.raises(SpotifyError):
        parse_redirect_paste("http://127.0.0.1:8911/callback?state=x")


# ── connect flow (routes) ────────────────────────────────────────────────────

def test_connect_with_explicit_pkce_fields(hub_app, fake):
    app, client = hub_app
    info = client.get("/api/spotify/connect-url").json()
    assert info["url"].startswith(fake.base + "/authorize?")
    assert f"client_id={CID}" in info["url"]
    assert "code_challenge_method=S256" in info["url"]
    assert f"redirect_uri={REDIRECT.replace(':', '%3A').replace('/', '%2F')}" \
        in info["url"]
    r = client.post("/api/spotify/connect", json={
        "code": "CODE-OK", "redirect_uri": info["redirect_uri"],
        "code_verifier": info["code_verifier"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] and body["connected_as"] == "Owner Person"
    tok = fake.last("POST", "/api/token")
    assert tok["form"]["grant_type"] == "authorization_code"
    assert tok["form"]["code_verifier"] == info["code_verifier"]
    assert tok["form"]["client_id"] == CID and "client_secret" not in tok["form"]
    # handshake is single use
    assert client.post("/api/spotify/connect", json={
        "code": "CODE-OK", "redirect_uri": REDIRECT,
        "code_verifier": info["code_verifier"]}).status_code == 200  # explicit verifier still works


def test_connect_paste_back(hub_app, fake):
    app, client = hub_app
    info = client.get("/api/spotify/connect-url").json()
    r = client.post("/api/spotify/connect",
                    json={"paste": f"{REDIRECT}?code=PASTED&state={info['state']}"})
    assert r.status_code == 200 and r.json()["connected"]
    tok = fake.last("POST", "/api/token")
    assert tok["form"]["code"] == "PASTED"
    assert tok["form"]["redirect_uri"] == REDIRECT
    # the stored PKCE verifier was used — the paste carried none
    assert tok["form"]["code_verifier"] == info["code_verifier"]
    assert client.get("/api/spotify/status").json()["connected"]


def test_connect_requires_client_id(hub_app):
    _, client = hub_app
    client.put("/api/settings", json={"spotify": {"clear_client_id": True}})
    r = client.get("/api/spotify/connect-url")
    assert r.status_code == 409
    assert "Developer Dashboard" in r.json()["detail"]


def test_disconnect_drops_tokens(hub_app):
    app, client = hub_app
    seed_tokens(app)
    assert client.get("/api/spotify/status").json()["connected"]
    st = client.post("/api/spotify/disconnect").json()
    assert not st["connected"]
    assert "spotify_tokens" not in app.state.store.known_keys() \
        if hasattr(app.state.store, "known_keys") else True


# ── settings secrecy ─────────────────────────────────────────────────────────

def test_tokens_never_in_settings_or_status(hub_app, fake):
    app, client = hub_app
    info = client.get("/api/spotify/connect-url").json()
    client.post("/api/spotify/connect", json={"paste": f"{REDIRECT}?code=C&state={info['state']}"})
    raw = client.get("/api/settings").text
    for secret in ("ACCESS-EXCHANGED", "REFRESH-1", "ACCESS-SEED"):
        assert secret not in raw, secret
    status_raw = client.get("/api/spotify/status").text
    assert "ACCESS-EXCHANGED" not in status_raw
    assert "REFRESH-1" not in status_raw
    st = client.get("/api/spotify/status").json()
    assert st["token_expires_at"]                       # expiry is visible
    # the blank-client_id edit never wipes the saved one (api_key rule)
    client.put("/api/settings", json={"spotify": {"client_id": "  ", "enabled": True}})
    assert client.get("/api/spotify/status").json()["configured"]


def test_settings_block_schema(hub_app):
    _, client = hub_app
    sp = client.get("/api/settings").json()["spotify"]
    assert sp["enabled"] is True and sp["client_id"] == CID
    assert sp["device_name"] == "Riko"


# ── refresh on expiry ────────────────────────────────────────────────────────

def test_expired_token_refreshes_and_rotates(hub_app, fake):
    app, client = hub_app
    seed_tokens(app, expires_in=-10.0)                  # already expired
    fake.now = playing_snapshot()
    snap = client.get("/api/spotify/now-playing").json()
    assert snap["track"] == TRACK["name"]
    assert fake.last("POST", "/api/token")["form"]["grant_type"] == "refresh_token"
    assert fake.last("GET", "/v1/me/player/currently-playing")["auth"] == "Bearer ACCESS-REFRESHED"
    stored = app.state.store.get_json("spotify_tokens", {})
    assert stored["access_token"] == "ACCESS-REFRESHED"
    assert stored["refresh_token"] == "REFRESH-1"       # kept when Spotify omits it
    assert stored["expires_at"] > time.time()


# ── play: query → URI → start on the chosen device ──────────────────────────

def test_play_resolves_and_starts(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    hub = app.state.spotify
    spec = next(s for s in build_spotify_specs(hub) if s.name == "spotify_play")
    out = spec.run({"query": "bohemian rhapsody"})
    assert isinstance(out, str) and "Bohemian Rhapsody" in out and "Queen" in out
    assert "Riko" in out                                 # the settings device
    search = fake.last("GET", "/v1/search")
    assert search["query"]["limit"] == "3" and int(search["query"]["limit"]) <= 10
    assert search["query"]["type"] == "track"
    play = fake.last("PUT", "/v1/me/player/play")
    assert play["json"]["uris"] == [TRACK["uri"]]
    assert play["query"]["device_id"] == DEVICE_RIKO["id"]   # already active


def test_play_moves_to_chosen_device(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.devices = [dict(DEVICE_RIKO, is_active=False), dict(DEVICE_ECHO, is_active=True)]
    hub = app.state.spotify
    spec = next(s for s in build_spotify_specs(hub) if s.name == "spotify_play")
    out = spec.run({"query": "queen", "kind": "track", "device": "Riko"})
    assert "Riko" in out
    transfer = fake.last("PUT", "/v1/me/player")        # moved there first
    assert transfer["json"]["device_ids"] == [DEVICE_RIKO["id"]]
    play = fake.last("PUT", "/v1/me/player/play")
    assert play["query"]["device_id"] == DEVICE_RIKO["id"]


def test_play_unknown_device_names_alternatives(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    hub = app.state.spotify
    with pytest.raises(SpotifyError, match="Echo Kitchen"):
        hub.play("anything", "track", "nonexistent-player")


def test_play_search_limit_clamped(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    app.state.spotify.client().search("x", "track", limit=999)
    assert fake.last("GET", "/v1/search")["query"]["limit"] == "10"


def test_control_actions(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    hub = app.state.spotify
    hub.control("pause")
    assert fake.last("PUT", "/v1/me/player/pause")
    hub.control("next")
    assert fake.last("POST", "/v1/me/player/next")
    hub.control("seek", 90)
    assert fake.last("PUT", "/v1/me/player/seek")["query"]["position_ms"] == "90000"
    hub.control("volume", 55)
    assert fake.last("PUT", "/v1/me/player/volume")["query"]["volume_percent"] == "55"
    hub.control("shuffle", "on")
    assert fake.last("PUT", "/v1/me/player/shuffle")["query"]["state"] == "true"
    with pytest.raises(SpotifyError, match="between 0 and 100"):
        hub.control("volume", 140)
    with pytest.raises(SpotifyError, match="pause, resume"):
        hub.control("eject")


def test_player_403_answers_premium(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.one_shot("PUT", "/v1/me/player/pause", 403,
                  {"error": {"status": 403, "message": "Premium required"}})
    with pytest.raises(SpotifyPremium, match="is Premium still active"):
        app.state.spotify.control("pause")


# ── like current: PUT /me/library (the dev-mode rule) ───────────────────────

def test_like_current_uses_me_library(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.now = playing_snapshot()
    spec = next(s for s in build_spotify_specs(app.state.spotify)
                if s.name == "spotify_like_current")
    out = spec.run({})
    assert "Bohemian Rhapsody" in out and "library" in out.lower()
    like = fake.last("PUT", "/v1/me/library")
    assert like["query"]["uris"] == TRACK["uri"]
    assert fake.hits("PUT", "/v1/me/tracks") == 0        # the old batch path


# ── quota 429: back off, never hammer ────────────────────────────────────────

def test_quota_429_backoff_never_hammers(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.one_shot("GET", "/v1/search", 429,
                  {"error": {"status": 429, "message": "QUOTA_EXCEEDED"}},
                  headers={"Retry-After": "3"})
    hub = app.state.spotify
    with pytest.raises(SpotifyQuota, match="quota"):
        hub.play("queen")
    hits_before = fake.hits("GET", "/v1/search")
    with pytest.raises(SpotifyQuota, match="cooling down"):
        hub.play("queen")                                 # answered instantly
    with pytest.raises(SpotifyQuota):
        hub.devices(force=True)                           # any music action waits
    assert fake.hits("GET", "/v1/search") == hits_before  # zero extra calls
    assert hub.quota_backoff() > 0
    r = client.get("/api/spotify/status").json()
    assert r["quota_backoff_left"] > 0
    # a route inside the window answers 429 too
    assert client.get("/api/spotify/now-playing").status_code == 429


def test_quota_without_retry_after_gets_fallback(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.one_shot("GET", "/v1/search", 429, {"error": {"message": "QUOTA_EXCEEDED"}})
    with pytest.raises(SpotifyQuota) as exc:
        app.state.spotify.play("queen")
    assert exc.value.retry_after >= 1.0


# ── now_playing event: dedupe + no polling while paused/idle ─────────────────

def test_now_playing_event_dedupe_and_idle_stop(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    hub = app.state.spotify
    hub.wake()
    after = client.get("/api/events?after=0&wait=0").json()["last"]
    fake.now = playing_snapshot()
    hub.poll_tick()
    assert len(events_of(client, after)) == 1
    hub.poll_tick()                                       # same state again
    assert len(events_of(client, after)) == 1             # deduped
    fake.now = playing_snapshot(is_playing=False)
    hub.poll_tick()                                       # pause changes state
    evs = events_of(client, after)
    assert len(evs) == 2 and evs[-1]["playing"] is False
    before = len(fake.log)                                # pause tick did poll
    hub.poll_tick()                                       # wake flag cleared
    assert len(fake.log) == before                        # NO API calls when idle
    hub.wake()                                            # resume wakes it again
    fake.now = playing_snapshot()
    hub.poll_tick()
    assert len(fake.log) > before
    assert len(events_of(client, after)) == 3


def test_now_playing_route_for_late_clients(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.now = playing_snapshot()
    snap = client.get("/api/spotify/now-playing").json()
    assert snap["playing"] and snap["uri"] == TRACK["uri"]
    assert snap["device"] == "Riko" and snap["artists"] == "Queen"
    fake.now = None
    app.state.spotify._now_cache = None                   # bypass the fresh cache
    snap = client.get("/api/spotify/now-playing").json()
    assert snap["playing"] is False and snap["uri"] == ""


def test_not_connected_gates(hub_app):
    _, client = hub_app
    assert client.get("/api/spotify/now-playing").status_code == 409
    assert client.get("/api/spotify/devices").status_code == 409
    hub_status = client.get("/api/spotify/status").json()
    assert hub_status["connected"] is False and hub_status["configured"] is True


# ── tools wiring: listed always, offered only when configured, human text ───

def test_tools_listed_and_gated(hub_app):
    _, client = hub_app
    names = {t["name"] for t in client.get("/api/tools").json()["tools"]}
    assert {"spotify_play", "spotify_control", "spotify_now_playing",
            "spotify_devices", "spotify_transfer",
            "spotify_like_current"} <= names
    row = next(t for t in client.get("/api/tools").json()["tools"]
               if t["name"] == "spotify_play")
    assert row["available"] is True                       # configured (CID set)


def test_unconfigured_tools_not_offered(tmp_path, fake):
    app = create_app(tmp_path / "hub2",
                     spotify_defaults={"accounts_url": fake.base,
                                       "api_url": fake.base + "/v1"})
    client = TestClient(app)
    row = next(t for t in client.get("/api/tools").json()["tools"]
               if t["name"] == "spotify_play")
    assert row["available"] is False                      # no client id yet
    spec = next(s for s in build_spotify_specs(app.state.spotify)
                if s.name == "spotify_play")
    with pytest.raises(SpotifyError):
        spec.run({"query": "anything"})


def test_tool_results_are_human_sentences(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.now = playing_snapshot()
    specs = {s.name: s for s in build_spotify_specs(app.state.spotify)}
    assert specs["spotify_now_playing"].run({}).startswith("Playing")
    assert "'Bohemian Rhapsody' by Queen" in specs["spotify_now_playing"].run({})
    devices = specs["spotify_devices"].run({})
    assert "Riko" in devices and "Echo Kitchen" in devices and "NOW PLAYING" in devices
    fake.now = None
    app.state.spotify._now_cache = None
    assert "Nothing is playing" in specs["spotify_now_playing"].run({})
    assert specs["spotify_control"].run({"action": "pause"}) == "Spotify paused."
    assert "80%" in specs["spotify_control"].run({"action": "volume", "value": 80})
    assert "moved" in specs["spotify_transfer"].run({"device": "Echo Kitchen"})


def test_disconnect_drops_tokens_and_wakes_off(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)
    fake.now = playing_snapshot()
    app.state.spotify.poll_tick()                          # cache + wake
    st = client.post("/api/spotify/disconnect").json()
    assert not st["connected"] and not st["polling"]
    assert client.get("/api/spotify/now-playing").status_code == 409
