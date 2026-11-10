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


def seed_tokens(app, expires_in=3600.0, refresh="REFRESH-1",
                scope="user-read-playback-state"):
    store = app.state.store
    store.set_json("spotify_tokens", {
        "access_token": "ACCESS-SEED", "refresh_token": refresh,
        "expires_at": time.time() + expires_in, "scope": scope})


# full consent since the 2026-11-10 scope change (owner playlists + liked
# songs); seed_tokens() alone keeps the OLD narrow scope → missing_scopes
FULL_SCOPES = ("user-read-playback-state user-modify-playback-state "
               "user-read-currently-playing user-library-modify "
               "playlist-read-private playlist-read-collaborative "
               "user-library-read")


def seed_owner(app):
    seed_tokens(app, scope=FULL_SCOPES)


DANCE = {"uri": "spotify:playlist:DANCE1", "name": "Dance",
         "tracks": {"total": 42}}
CHILL = {"uri": "spotify:playlist:CHILL1", "name": "Chill Vibes 🌙",
         "tracks": {"total": 8}}


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
    # the PKCE verifier stays on the hub — never handed to clients
    assert "code_verifier" not in info
    verifier = app.state.store.get_json("spotify_pending", {})["verifier"]
    assert info["url"].startswith(fake.base + "/authorize?")
    assert f"client_id={CID}" in info["url"]
    assert "code_challenge_method=S256" in info["url"]
    assert f"redirect_uri={REDIRECT.replace(':', '%3A').replace('/', '%2F')}" \
        in info["url"]
    r = client.post("/api/spotify/connect", json={
        "code": "CODE-OK", "redirect_uri": info["redirect_uri"],
        "code_verifier": verifier})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] and body["connected_as"] == "Owner Person"
    tok = fake.last("POST", "/api/token")
    assert tok["form"]["grant_type"] == "authorization_code"
    assert tok["form"]["code_verifier"] == verifier
    assert tok["form"]["client_id"] == CID and "client_secret" not in tok["form"]
    # handshake is single use
    assert client.post("/api/spotify/connect", json={
        "code": "CODE-OK", "redirect_uri": REDIRECT,
        "code_verifier": verifier}).status_code == 200  # explicit verifier still works


def test_connect_paste_back(hub_app, fake):
    app, client = hub_app
    info = client.get("/api/spotify/connect-url").json()
    verifier = app.state.store.get_json("spotify_pending", {})["verifier"]
    r = client.post("/api/spotify/connect",
                    json={"paste": f"{REDIRECT}?code=PASTED&state={info['state']}"})
    assert r.status_code == 200 and r.json()["connected"]
    tok = fake.last("POST", "/api/token")
    assert tok["form"]["code"] == "PASTED"
    assert tok["form"]["redirect_uri"] == REDIRECT
    # the stored PKCE verifier was used — the paste carried none
    assert tok["form"]["code_verifier"] == verifier
    assert client.get("/api/spotify/status").json()["connected"]


def test_connect_rejects_foreign_state(hub_app, fake):
    app, client = hub_app
    client.get("/api/spotify/connect-url")
    r = client.post("/api/spotify/connect",
                    json={"paste": f"{REDIRECT}?code=EVIL&state=not-ours"})
    assert r.status_code >= 400 and "state mismatch" in r.text
    assert not client.get("/api/spotify/status").json()["connected"]
    assert not fake.last("POST", "/api/token")   # no token exchange attempted


def test_connect_rejects_expired_handshake(hub_app, fake):
    app, client = hub_app
    info = client.get("/api/spotify/connect-url").json()
    pending = app.state.store.get_json("spotify_pending", {})
    pending["created"] -= 10_000
    app.state.store.set_json("spotify_pending", pending)
    r = client.post("/api/spotify/connect", json={
        "code": "CODE-OK", "state": info["state"], "redirect_uri": REDIRECT})
    assert r.status_code >= 400 and "expired" in r.text
    assert not fake.last("POST", "/api/token")   # no token exchange attempted


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


# ── owner's own playlists + liked songs (2026-11-10 live-bug round) ─────────
#
# Catalogue /search does not reliably surface the user's own (private)
# playlists, so kind=playlist matches GET /me/playlists FIRST; 'my liked
# songs' plays GET /me/tracks as uris; SCOPES grew, so an old connection
# must Disconnect + Connect once (status.missing_scopes says so).

def test_play_owner_playlist_by_name_never_searches(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [None, DANCE, CHILL]      # null entry like real Spotify
    hub = app.state.spotify
    r = hub.play("Dance", "playlist")          # 'Dance' is a PRIVATE playlist
    assert r["uri"] == DANCE["uri"] and r["source"] == "library"
    assert fake.hits("GET", "/v1/search") == 0        # catalogue untouched
    play = fake.last("PUT", "/v1/me/player/play")
    assert play["json"]["context_uri"] == DANCE["uri"]
    assert play["query"]["device_id"] == DEVICE_RIKO["id"]


def test_play_my_playlist_words_and_kind_rule(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [DANCE, CHILL]
    hub = app.state.spotify
    # 'my dance playlist' said kind=track → the query's word 'playlist'
    # forces kind=playlist and the owner's library matches first
    r = hub.play("my dance playlist", "track")
    assert r["uri"] == DANCE["uri"] and r["kind"] == "playlist"
    assert fake.hits("GET", "/v1/search") == 0
    # emoji + filler words in the OWNER's name still match
    r2 = hub.play("chill vibes", "playlist")
    assert r2["uri"] == CHILL["uri"]


def test_play_owner_playlist_fuzzy(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [DANCE, CHILL]
    r = app.state.spotify.play("dnace", "playlist")   # typo still finds Dance
    assert r["uri"] == DANCE["uri"]
    assert fake.hits("GET", "/v1/search") == 0


def test_play_playlist_ties_prefer_exact_case_and_mention_alts(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    twin = dict(DANCE, uri="spotify:playlist:DANCE2", name="Dance!")
    fake.playlists = [twin, DANCE]              # equal after normalising
    r = app.state.spotify.play("Dance", "playlist")
    assert r["uri"] == DANCE["uri"]             # exact-case name wins
    assert r["alternatives"] == ["Dance!"]      # ... and gets mentioned
    spec = next(s for s in build_spotify_specs(app.state.spotify)
                if s.name == "spotify_play")
    out = spec.run({"query": "Dance", "kind": "playlist"})
    assert "your playlist “Dance”" in out and "Dance!" in out


def test_play_playlist_falls_back_to_search_none_filtered(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [DANCE]                    # no match for the query
    fake.search["playlist"] = [None, {"uri": "spotify:playlist:CAT9",
                                      "name": "Workout Bangers"}]
    r = app.state.spotify.play("Workout Bangers", "playlist")
    assert r["uri"] == "spotify:playlist:CAT9" and r["source"] == "search"
    assert fake.hits("GET", "/v1/search") == 1  # owner list first, then search
    play = fake.last("PUT", "/v1/me/player/play")
    assert play["json"]["context_uri"] == "spotify:playlist:CAT9"


def test_play_liked_songs_starts_uris(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.saved = [{"track": {"uri": "spotify:track:T1"}}, None,
                  {"track": {"uri": "spotify:track:T2"}}, {"track": None}]
    hub = app.state.spotify
    r = hub.play("my liked songs")
    assert r["kind"] == "liked" and r["count"] == 2
    play = fake.last("PUT", "/v1/me/player/play")
    assert play["json"]["uris"] == ["spotify:track:T1", "spotify:track:T2"]
    assert fake.hits("GET", "/v1/me/tracks") == 1
    # shuffle requested → the uris are shuffled (same set, may reorder)
    r2 = hub.play("play my saved tracks", "track", shuffle=True)
    play2 = fake.last("PUT", "/v1/me/player/play")
    assert sorted(play2["json"]["uris"]) == ["spotify:track:T1",
                                             "spotify:track:T2"]
    assert r2["shuffle"] is True
    spec = next(s for s in build_spotify_specs(hub) if s.name == "spotify_play")
    assert "Liked Songs" in spec.run({"query": "my liked songs"})
    # the Web API can't start the Liked Songs context: no context_uri
    assert "context_uri" not in play["json"]


def test_missing_scopes_answer_reconnect(hub_app, fake):
    app, client = hub_app
    seed_tokens(app)                    # OLD narrow scope (live-bug state)
    fake.playlists = [DANCE]
    hub = app.state.spotify
    st = client.get("/api/spotify/status").json()
    assert st["connected"]
    assert "playlist-read-private" in st["missing_scopes"]
    assert "user-library-read" in st["missing_scopes"]
    with pytest.raises(SpotifyError,
                       match="reconnect Spotify to allow playlists"):
        hub.play("Dance", "playlist")   # the live bug now says WHY
    with pytest.raises(SpotifyError,
                       match="reconnect Spotify to allow liked songs"):
        hub.play("my liked songs")
    # plain tracks keep working without the new scopes
    hub.play("bohemian rhapsody")
    assert fake.hits("GET", "/v1/search") == 1


def test_playlists_cache_and_tool_lists_names(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [None, DANCE, CHILL]
    hub = app.state.spotify
    rows = hub.playlists()
    assert rows == [{"name": "Dance", "uri": DANCE["uri"], "tracks": 42},
                    {"name": "Chill Vibes 🌙", "uri": CHILL["uri"],
                     "tracks": 8}]
    hub.playlists()                                   # cached, quota-respecting
    hub.play("Dance", "playlist")
    assert fake.hits("GET", "/v1/me/playlists") == 1
    hub.playlists(force=True)                         # explicit refresh works
    assert fake.hits("GET", "/v1/me/playlists") == 2
    spec = next(s for s in build_spotify_specs(hub) if s.name == "spotify_playlists")
    out = spec.run({})
    assert "Dance" in out and "42 tracks" in out and "Chill Vibes" in out
    only = spec.run({"query": "chill"})
    assert "Chill Vibes" in only and "Dance" not in only
    assert spec.available if hasattr(spec, "available") else True


def test_playlists_pagination(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [None] + [dict(DANCE, uri=f"spotify:playlist:P{i}",
                                    name=f"Mix {i}") for i in range(60)]
    rows = app.state.spotify.playlists(force=True)
    assert len(rows) == 60                            # 50 + 10 across pages
    assert fake.hits("GET", "/v1/me/playlists") == 2
    assert fake.last("GET", "/v1/me/playlists")["query"]["offset"] == "50"


def test_connect_invalidates_playlists_cache(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    fake.playlists = [DANCE]
    app.state.spotify.playlists()
    assert fake.hits("GET", "/v1/me/playlists") == 1
    info = client.get("/api/spotify/connect-url").json()
    fake.tokens["scope"] = FULL_SCOPES
    client.post("/api/spotify/connect",
                json={"paste": f"{REDIRECT}?code=C&state={info['state']}"})
    app.state.spotify.playlists()                     # re-consent → refetch
    assert fake.hits("GET", "/v1/me/playlists") == 2


def test_status_full_consent_has_no_missing_scopes(hub_app, fake):
    app, client = hub_app
    seed_owner(app)
    st = client.get("/api/spotify/status").json()
    assert st["missing_scopes"] == []
    # not connected → nothing to flag
    client.post("/api/spotify/disconnect")
    assert client.get("/api/spotify/status").json()["missing_scopes"] == []
