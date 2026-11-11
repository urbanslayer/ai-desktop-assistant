"""Spotify over the Web API (music phase 1, docs/SPOTIFY_NOTES.md).

The hub owns ONE Spotify connection under the owner's Developer Dashboard
app: OAuth Authorization Code + PKCE (client id only — no secret), token
exchange/refresh, and the Player API. The hub never streams audio; it only
controls Spotify Connect devices (desktop playback is spotifyd, device name
"Riko").

Verified 2026 dev-mode rules this code obeys (docs/SPOTIFY_NOTES.md):
- redirect URIs must be HTTPS or a 127.0.0.1 literal — so the callback
  lives on the DESKTOP (http://127.0.0.1:8911/callback), which hands the
  code to the hub; the web page can also paste the redirected URL back.
- search `limit` is capped at 10 (default 5); we clamp, never exceed.
- no batch-fetch endpoints: tracks are fetched one by one.
- likes go through PUT /me/library?uris=... (the old /me/tracks is gone).
- 429 QUOTA_EXCEEDED: back off for the advertised window and never hammer
  (a hard quota window on a personal dev app is minutes).
- GET /me no longer reports Premium, so a 403/404 on a PLAYER call answers
  "is Premium still active?".

Base URLs are configurable (settings `spotify.accounts_url` /
`spotify.api_url`, env defaults RIKO_SPOTIFY_ACCOUNTS_URL /
RIKO_SPOTIFY_API_URL) so tests run against an in-repo fake HTTP server.

Tokens live in the hub store under their own key (`spotify_tokens`) and are
write-only like the LLM api_key: GET /api/settings never returns them;
/api/spotify/status only reports booleans/expiry/scope.

`now_playing` polling (event + GET for late clients): while playing, poll at
most every POLL_S (5 s). While NOT playing the poller keeps a SLOW idle
watch (settings `spotify.idle_poll_s`, default 30 s, 0 disables, floor
MIN_IDLE_POLL_S) so playback started from a phone / another Spotify app is
noticed too — but only while someone cares (see `interested`), and never
during a quota back-off. The fast poll (POLL_S) resumes the moment a poll
reports playing; hub start takes one immediate snapshot so state after a
restart is right. Interest heuristic: a desktop client is registered+online
(hub/server.py wires clients_hub), OR a client fetched
GET /api/spotify/now-playing within the last PAGE_INTEREST_S (a page that
just opened the ticker asks; a page nobody opened never asks).
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import random
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse

import requests

DEFAULT_ACCOUNTS_BASE = "https://accounts.spotify.com"
DEFAULT_API_BASE = "https://api.spotify.com/v1"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8911/callback"
DEFAULT_DEVICE_NAME = "Riko"
# scopes the tools need: player state + control, currently-playing for the
# event/late clients, library writes for spotify_like_current, and (2026-11-10,
# owner-playlist round) the owner's own playlists + saved tracks — catalogue
# /search does NOT reliably surface the user's own (esp. private) playlists,
# so 'play my X playlist' and 'my liked songs' need these. Changing SCOPES
# means the owner must Disconnect + Connect once to re-consent (status
# exposes missing_scopes; the card and tool errors say to reconnect).
SCOPES = ("user-read-playback-state user-modify-playback-state "
          "user-read-currently-playing user-library-modify "
          "playlist-read-private playlist-read-collaborative "
          "user-library-read")
PLAYLIST_SCOPES = ("playlist-read-private", "playlist-read-collaborative")
LIKED_SCOPES = ("user-library-read",)
SEARCH_LIMIT_MAX = 10          # dev-mode hard cap (docs/SPOTIFY_NOTES.md)
POLL_S = 5.0                   # now_playing poll ceiling while playing
MIN_POLL_S = 1.0               # test floor
IDLE_POLL_S = 30.0             # slow watch while NOT playing (0 disables)
MIN_IDLE_POLL_S = 15.0         # quota floor for the idle watch
PAGE_INTEREST_S = 120.0        # a now-playing GET keeps the idle watch alive
                               # this long (a ticker page that just opened asks)
QUOTA_FALLBACK_S = 30.0        # 429 without a usable Retry-After
REFRESH_SLACK_S = 60.0         # refresh this long before expiry
DEVICE_CACHE_S = 60.0          # device list cache (quota discipline)
USER_AGENT = "riko-hub/1 spotify"
PENDING_TTL_S = 900.0          # a PKCE handshake is single-use and short
PLAYLIST_CACHE_S = 300.0       # /me/playlists cache (~5 min; quota discipline)
OWN_PAGE_LIMIT = 50            # /me/playlists + /me/tracks page size


class SpotifyError(RuntimeError):
    """Anything the model/owner should be told about, in plain words."""


class SpotifyNotConfigured(SpotifyError):
    """No client id yet: the Developer Dashboard app is missing."""


class SpotifyNotConnected(SpotifyError):
    """Configured but nobody has done the OAuth dance yet."""


class SpotifyQuota(SpotifyError):
    """429 QUOTA_EXCEEDED — the hub waits out the window; callers must not
    hammer. `retry_after` seconds."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class SpotifyPremium(SpotifyError):
    """403/404 on a player call: GET /me no longer reports Premium, so the
    subscription is the first suspect (docs/SPOTIFY_NOTES.md)."""


# ── pure helpers (scopes, owner-name matching) ──────────────────────────────

REQUIRED_SCOPES = tuple(SCOPES.split())


def missing_scopes_for(granted: str) -> list[str]:
    """Required scopes absent from the granted `scope` string. An empty
    granted string (old connection, pre-scope-storage) counts as everything
    missing — the owner must re-consent once."""
    g = set(str(granted or "").split())
    return [s for s in REQUIRED_SCOPES if s not in g]


NAME_STOPWORDS = {"my", "the", "playlist"}


def normalise_name(text: str) -> str:
    """Playlist-name matcher key: casefold, drop emoji/punctuation, drop the
    filler words 'my', 'the', 'playlist' ('My Dance 🕺 Playlist' → 'dance')."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    chars = []
    for ch in s.casefold():
        cat = unicodedata.category(ch)
        if cat == "Mn":                    # stray combining accent after NFKD
            continue
        chars.append(ch if cat[0] in ("L", "N") or ch.isspace() else " ")
    return " ".join(w for w in "".join(chars).split()
                    if w not in NAME_STOPWORDS)


def match_playlists(query: str, playlists: list) -> tuple[dict | None, list[str]]:
    """Pick the user's own playlist a spoken query means: exact > startswith
    > contains > fuzzy (difflib ratio ≥ 0.8). Ties: exact-case name first,
    else the first tied entry. Returns (playlist | None, [other tied names])
    so callers can mention the alternatives."""
    nq = normalise_name(query)
    if not nq:
        return None, []
    scored: list[tuple[int, dict]] = []
    for pl in playlists or []:
        if not pl:                         # Spotify interleaves null entries
            continue
        nn = normalise_name(pl.get("name") or "")
        if not nn:
            continue
        if nn == nq:
            rank = 0
        elif nn.startswith(nq):
            rank = 1
        elif nq in nn or nn in nq:
            rank = 2
        elif difflib.SequenceMatcher(None, nq, nn).ratio() >= 0.8:
            rank = 3
        else:
            continue
        scored.append((rank, pl))
    if not scored:
        return None, []
    best_rank = min(r for r, _ in scored)
    ties = [pl for r, pl in scored if r == best_rank]
    raw = str(query or "").strip()
    exact = [p for p in ties
             if str(p.get("name") or "").strip() == raw]
    best = exact[0] if exact else ties[0]
    alts = [str(p.get("name") or "") for p in ties if p is not best]
    return best, alts


_LIKED_ADJ = re.compile(r"\b(liked|loved|saved|favourite|favourites|favorite|favorites)\b")
_SONG_NOUN = re.compile(r"\b(songs?|tracks?|music)\b")


def is_liked_songs(query: str) -> bool:
    """'my liked songs', 'play my saved tracks', 'favourites' …"""
    n = normalise_name(query)
    if not n:
        return False
    if bool(_LIKED_ADJ.search(n) and _SONG_NOUN.search(n)):
        return True
    return bool(re.search(r"\bfavou?rites\b", n))


# ── PKCE + authorize URL (pure, testable) ───────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str,
                  code_challenge: str, *, scopes: str = SCOPES,
                  accounts_base: str = DEFAULT_ACCOUNTS_BASE) -> str:
    """The URL the owner opens in their browser (the desktop app or the web
    card shows it; Spotify redirects back to the loopback listener)."""
    q = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "scope": scopes,
        "state": state,
    })
    return f"{accounts_base.rstrip('/')}/authorize?{q}"


def parse_redirect_paste(paste: str) -> dict:
    """Accept the FULL redirected URL (`http://127.0.0.1:8911/callback?code=…
    &state=…`), a bare query string, or a bare code (paste-back fallback,
    docs/SPOTIFY_NOTES.md). Returns {code, state, redirect_uri}; redirect_uri
    is the URL without its query (must match the authorize request).
    Raises SpotifyError on an error= redirect or a missing code."""
    raw = str(paste or "").strip()
    if not raw:
        raise SpotifyError("nothing pasted — paste the whole URL from the "
                           "browser's address bar after the Spotify login")
    looks_like_query = "=" in raw and (
        "&" in raw or raw.startswith(("code=", "state=", "error=")))
    if "://" in raw or raw.startswith("/") or "?" in raw or looks_like_query:
        parsed = urllib.parse.urlsplit(raw if "://" in raw
                                       else "http://127.0.0.1:8911/callback"
                                       + (raw if raw.startswith("/")
                                          else "?" + raw))
        qs = urllib.parse.parse_qs(parsed.query)
        redirect_uri = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", ""))
    else:
        qs, redirect_uri = {"code": [raw]}, ""
    err = (qs.get("error") or [""])[0]
    if err:
        desc = (qs.get("error_description") or [err])[0]
        raise SpotifyError(f"Spotify login was not completed: {desc}")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise SpotifyError("no ?code= in what you pasted — paste the whole "
                           "redirected URL from the address bar")
    return {"code": code, "state": (qs.get("state") or [""])[0],
            "redirect_uri": redirect_uri}


# ── the Web API client ──────────────────────────────────────────────────────

def _error_message(r) -> str:
    """Spotify's error body message ('Player command failed: Restriction
    violated'), '' when the body isn't JSON or has no message."""
    try:
        return str((r.json().get("error") or {}).get("message") or "")
    except ValueError:
        return ""


def _restriction_violated(r) -> bool:
    """The 403 Spotify sends to the FIRST play aimed at a just-registered
    device — a Connect race, not a lapsed Premium subscription."""
    return "restriction violated" in _error_message(r).lower()


def _target_device(params: dict | None, body: dict | None) -> str:
    """The device the command targets (play passes ?device_id=, transfer
    passes body.device_ids[0]); '' when the command names none."""
    dev = str((params or {}).get("device_id") or "").strip()
    if dev:
        return dev
    ids = (body or {}).get("device_ids")
    if isinstance(ids, list) and ids:
        return str(ids[0] or "").strip()
    return ""


class SpotifyClient:
    """One logged-in user's Web API calls. `settings` is the merged spotify
    settings block; `tokens` the stored token object; `save(tokens)` (optional)
    persists rotated tokens. `clock` is injectable for expiry tests."""

    def __init__(self, settings: dict, tokens: dict, save=None,
                 clock=time.monotonic):
        s = settings or {}
        t = tokens or {}
        self.client_id = str(s.get("client_id") or "").strip()
        self.accounts_base = (str(s.get("accounts_url") or "").strip()
                              or DEFAULT_ACCOUNTS_BASE)
        self.api_base = str(s.get("api_url") or "").strip() or DEFAULT_API_BASE
        self.tokens = dict(t)
        self._save = save
        self._clock = clock
        self._refresh_lock = threading.Lock()

    # -- tokens ---------------------------------------------------------
    def _expired(self) -> bool:
        try:
            return float(self.tokens.get("expires_at") or 0) <= \
                time.time() + REFRESH_SLACK_S
        except (TypeError, ValueError):
            return True

    def access_token(self) -> str:
        token = str(self.tokens.get("access_token") or "")
        if not token or self._expired():
            token = self.refresh()
        return token

    def refresh(self) -> str:
        refresh_token = str(self.tokens.get("refresh_token") or "")
        if not self.client_id:
            raise SpotifyNotConfigured(
                "Spotify client id is not set — add it under Settings > "
                "Spotify (Developer Dashboard app, docs/SPOTIFY_NOTES.md)")
        if not refresh_token:
            raise SpotifyNotConnected(
                "Spotify is not connected — use Settings > Spotify > "
                "Connect (or the paste-back box) once")
        with self._refresh_lock:
            if self._expired() or not self.tokens.get("access_token"):
                data = self._token_request({
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                })
                self._apply_tokens(data, keep_refresh=refresh_token)
        return str(self.tokens["access_token"])

    def exchange_code(self, code: str, redirect_uri: str,
                      code_verifier: str) -> dict:
        """Authorization-code + PKCE exchange (no client secret)."""
        data = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        })
        self._apply_tokens(data)
        return dict(self.tokens)

    def _token_request(self, form: dict) -> dict:
        url = self.accounts_base.rstrip("/") + "/api/token"
        try:
            r = requests.post(url, data=form, timeout=15,
                              headers={"User-Agent": USER_AGENT})
        except requests.RequestException as e:
            raise SpotifyError(f"Spotify's token server did not answer ({e})") from e
        if r.status_code != 200:
            try:
                body = r.json()
            except ValueError:
                body = {}
            desc = str(body.get("error_description") or body.get("error")
                       or f"HTTP {r.status_code}")
            hint = {"invalid_grant": " (the code was already used or expired "
                                     "— start Connect again)"}.get(str(body.get("error")), "")
            raise SpotifyError(f"Spotify token exchange failed: {desc}{hint}")
        try:
            return r.json()
        except ValueError as e:
            raise SpotifyError("Spotify's token server returned non-JSON") from e

    def _apply_tokens(self, data: dict, keep_refresh: str = "") -> None:
        try:
            expires_in = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self.tokens["access_token"] = str(data.get("access_token") or "")
        self.tokens["refresh_token"] = (str(data.get("refresh_token") or "")
                                        or keep_refresh
                                        or str(self.tokens.get("refresh_token") or ""))
        self.tokens["expires_at"] = time.time() + max(30.0, expires_in - 5.0)
        if data.get("scope"):
            self.tokens["scope"] = str(data["scope"])
        if self._save is not None:
            self._save(dict(self.tokens))

    # -- HTTP -----------------------------------------------------------
    def api(self, method: str, path: str, *, params: dict | None = None,
            body: dict | None = None, player: bool = False):
        """One Web API call. Returns the parsed JSON body (None for 204).
        401 → refresh once + retry; 429 → SpotifyQuota; player 403/404 →
        SpotifyPremium; other non-2xx → SpotifyError.

        Exception (docs/SOLOIST.md): a player-command 403 whose body says
        'Restriction violated' is Spotify's known first-play-to-a-
        just-registered-device answer, NOT a lapsed subscription — the hub
        transfers playback to the target device and retries ONCE before
        raising. Premium is only blamed when the body says something else
        or that retry also fails."""
        url = self.api_base.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.access_token()}",
                   "User-Agent": USER_AGENT}
        r = self._send(method, url, headers, params, body)
        if r.status_code == 401:
            # a rotated/revoked token: refresh once, retry once
            headers["Authorization"] = f"Bearer {self.refresh()}"
            r = self._send(method, url, headers, params, body)
        if player and r.status_code == 403 and _restriction_violated(r):
            device = _target_device(params, body)
            if not device:
                raise SpotifyError(
                    "Spotify refused the player command: 'Player command "
                    "failed: Restriction violated' (HTTP 403) and the hub "
                    "has no target device to transfer to — start the "
                    "desktop player (tray Music > Start player) or name a "
                    "device explicitly")
            # first play to a just-registered device: activate it, retry once
            try:
                self._send("PUT", self.api_base.rstrip("/") + "/me/player",
                           headers, None, {"device_ids": [device],
                                           "play": False})
            except SpotifyError:
                pass            # surface the original refusal via the retry
            r = self._send(method, url, headers, params, body)
        return self._result(method, path, r, player)

    def _send(self, method, url, headers, params, body):
        try:
            return requests.request(method, url, headers=headers,
                                    params=params, json=body, timeout=15)
        except requests.RequestException as e:
            raise SpotifyError(f"Spotify did not answer ({e})") from e

    def _result(self, method, path, r, player):
        if r.status_code == 429:
            retry = QUOTA_FALLBACK_S
            try:
                retry = max(1.0, float(r.headers.get("Retry-After") or 0))
            except (TypeError, ValueError):
                pass
            if retry <= 0:
                retry = QUOTA_FALLBACK_S
            raise SpotifyQuota(
                "Spotify's dev-mode quota is used up — the hub will hold off "
                f"until it resets (~{int(retry)}s); don't retry before that",
                retry)
        if r.status_code in (403, 404) and player:
            raise SpotifyPremium(
                f"Spotify refused the player command (HTTP {r.status_code}) "
                "— is Premium still active? The app stops if the "
                "subscription lapses (docs/SPOTIFY_NOTES.md)")
        if r.status_code == 204:
            return None
        if r.status_code not in (200, 201, 202):
            detail = ""
            try:
                detail = str((r.json().get("error") or {}).get("message")
                             or r.text[:120])
            except ValueError:
                detail = r.text[:120]
            raise SpotifyError(f"Spotify answered HTTP {r.status_code} for "
                               f"{method} {path}"
                               + (f": {detail}" if detail else ""))
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise SpotifyError("Spotify returned non-JSON") from e

    # -- endpoints used by the tools ------------------------------------
    def me(self) -> dict:
        return self.api("GET", "/me") or {}

    def search(self, query: str, kind: str, limit: int = 3) -> list[dict]:
        """One search type, limit clamped to the dev-mode cap of 10.
        Returns the raw items (already the right type for `kind`).
        Spotify interleaves NULL entries in items lists — always filtered."""
        limit = max(1, min(SEARCH_LIMIT_MAX, int(limit)))
        data = self.api("GET", "/search",
                        params={"q": query, "type": kind, "limit": limit})
        items = ((data or {}).get(kind) or {}).get("items") or []
        return [i for i in items if i]

    def my_playlists(self, limit: int = OWN_PAGE_LIMIT) -> list[dict]:
        """GET /me/playlists, paginated (limit + offset) — the ONLY
        dependable way to see the owner's own (esp. private) playlists;
        catalogue /search does not reliably surface them. Null entries in
        items are filtered like everywhere else."""
        out: list[dict] = []
        offset = 0
        while True:
            data = self.api("GET", "/me/playlists",
                            params={"limit": int(limit), "offset": offset}) or {}
            page = [p for p in (data.get("items") or []) if p]
            out.extend(page)
            offset += int(limit)
            if not data.get("next") or not page or offset > 1000:
                break                       # paginate, hard stop at 20 pages
        return out

    def saved_tracks(self, limit: int = OWN_PAGE_LIMIT) -> list[str]:
        """First page of GET /me/tracks → track URIs. The Web API cannot
        start the Liked Songs context directly, so liked-songs playback
        passes these URIs to start_playback."""
        data = self.api("GET", "/me/tracks", params={"limit": int(limit)}) or {}
        uris = []
        for row in data.get("items") or []:
            if not row:
                continue
            uri = str(((row.get("track") or {}).get("uri")) or "")
            if uri.startswith("spotify:track:"):
                uris.append(uri)
        return uris

    def current_playback(self) -> dict | None:
        """GET /me/player/currently-playing → None when nothing plays."""
        url = self.api_base.rstrip("/") + "/me/player/currently-playing"
        headers = {"Authorization": f"Bearer {self.access_token()}",
                   "User-Agent": USER_AGENT}
        r = self._send("GET", url, headers, None, None)
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code != 200:
            return self._result("GET", "/me/player/currently-playing",
                                r, player=True)
        return r.json()

    def player_devices(self) -> list[dict]:
        return (self.api("GET", "/me/player/devices") or {}).get("devices") or []

    def transfer(self, device_id: str, play: bool = True) -> None:
        self.api("PUT", "/me/player", body={"device_ids": [device_id],
                                            "play": bool(play)}, player=True)

    def start_playback(self, *, uris: list | None = None,
                       context_uri: str = "", position_ms: int | None = None,
                       device_id: str = "") -> None:
        body: dict = {}
        if uris:
            body["uris"] = list(uris)
        elif context_uri:
            body["context_uri"] = context_uri
        if position_ms is not None:
            body["position_ms"] = int(position_ms)
        params = {"device_id": device_id} if device_id else None
        self.api("PUT", "/me/player/play", params=params, body=body,
                 player=True)

    def pause(self) -> None:
        self.api("PUT", "/me/player/pause", player=True)

    def next_track(self) -> None:
        self.api("POST", "/me/player/next", player=True)

    def previous_track(self) -> None:
        self.api("POST", "/me/player/previous", player=True)

    def seek(self, position_ms: int) -> None:
        self.api("PUT", "/me/player/seek",
                 params={"position_ms": int(position_ms)}, player=True)

    def set_volume(self, percent: int) -> None:
        self.api("PUT", "/me/player/volume",
                 params={"volume_percent": int(percent)}, player=True)

    def set_shuffle(self, on: bool) -> None:
        self.api("PUT", "/me/player/shuffle",
                 params={"state": "true" if on else "false"}, player=True)

    def add_to_library(self, uris: list[str]) -> None:
        """Generic PUT /me/library (the /me/tracks batch is gone in dev mode)."""
        self.api("PUT", "/me/library", params={"uris": ",".join(uris)})


# ── the hub-side orchestrator ───────────────────────────────────────────────

def track_snapshot(cp: dict | None) -> dict:
    """currently-playing payload → the compact now_playing shape clients get:
    {playing, uri, track, artists, album, duration_ms, position_ms, device,
    context_uri}. Nothing playing → {playing: False, uri: ''}."""
    if not cp:
        return {"playing": False, "uri": "", "track": "", "artists": "",
                "album": "", "duration_ms": 0, "position_ms": 0,
                "device": "", "context_uri": ""}
    item = cp.get("item") or {}
    device = cp.get("device") or {}
    return {
        "playing": bool(cp.get("is_playing")),
        "uri": str(item.get("uri") or ""),
        "track": str(item.get("name") or ""),
        "artists": ", ".join(str(a.get("name") or "")
                             for a in (item.get("artists") or []) if a.get("name")),
        "album": str((item.get("album") or {}).get("name") or ""),
        "duration_ms": int(item.get("duration_ms") or 0),
        "position_ms": int(cp.get("progress_ms") or 0),
        "device": str(device.get("name") or ""),
        "context_uri": str((cp.get("context") or {}).get("uri") or ""),
    }


class SpotifyHub:
    """Owns tokens, quota back-off, the device cache, the now_playing poller
    and the event publishing. The routes and the tools are thin shells.

    `interest_hook` (optional callable) says whether a DESKTOP client is
    online right now — hub/server.py wires clients_hub into it. Together
    with the in-hub now-playing-request window (see `interested`) it gates
    the idle watch, so a hub nobody watches burns zero Spotify calls."""

    def __init__(self, store, events, settings_fn, *, defaults: dict | None = None,
                 on_change=None, interest_hook=None, clock=time.monotonic):
        self.store = store
        self.events = events
        self.settings_fn = settings_fn      # -> merged spotify settings dict
        self.on_change = on_change          # bump_state hook (connect/disconnect)
        self.interest_hook = interest_hook  # () -> bool: a desktop is online
        self._clock = clock                 # fake-clock seam (idle cadence)
        d = dict(defaults or {})
        self.poll_s = max(MIN_POLL_S, float(d.get("poll_s") or POLL_S))
        self._quota_until = 0.0             # monotonic deadline after a 429
        self._quota_left = 0.0
        self._lock = threading.Lock()
        self._devices_cache: tuple[float, list] = (0.0, [])
        self._playlists_cache: tuple[float, list] = (0.0, [])
        self._now_cache: dict | None = None
        self._last_sig: tuple | None = None
        self._wake = threading.Event()      # set = fast poll (something is playing)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._np_requested = 0.0            # last GET now-playing (clock)

    # -- settings / state -------------------------------------------------
    def settings(self) -> dict:
        return dict(self.settings_fn() or {})

    def configured(self) -> bool:
        return bool(str(self.settings().get("client_id") or "").strip())

    def enabled(self) -> bool:
        return bool(self.settings().get("enabled"))

    def tokens(self) -> dict:
        return self.store.get_json("spotify_tokens", {}) or {}

    def connected(self) -> bool:
        t = self.tokens()
        return bool(t.get("refresh_token"))

    def require(self) -> None:
        """Guard every music action: enabled in settings + configured + connected."""
        if not self.configured():
            raise SpotifyNotConfigured(
                "Spotify isn't set up yet: create the Developer Dashboard app "
                "(docs/SPOTIFY_NOTES.md), put its Client ID in Settings > "
                "Spotify, then Connect")
        if not self.enabled():
            raise SpotifyError("Spotify is switched off — tick Spotify in "
                               "the hub Settings first")
        if not self.connected():
            raise SpotifyNotConnected(
                "Spotify isn't connected yet — open the hub's Settings page, "
                "Spotify card, and press Connect")

    def client(self) -> SpotifyClient:
        settings = self.settings()
        if not str(settings.get("client_id") or "").strip():
            raise SpotifyNotConfigured(
                "Spotify client id is not set — add it under Settings > "
                "Spotify (Developer Dashboard app, docs/SPOTIFY_NOTES.md)")
        return SpotifyClient(settings, self.tokens(),
                             save=lambda t: self.store.set_json("spotify_tokens", t))

    # -- scopes ---------------------------------------------------------------
    def missing_scopes(self) -> list[str]:
        """Required scopes the granted token lacks. The owner re-consents by
        Disconnect + Connect once (SCOPES changed 2026-11-10)."""
        if not self.connected():
            return []
        return missing_scopes_for(str(self.tokens().get("scope") or ""))

    def require_scope(self, needed: tuple[str, ...], what: str) -> None:
        """Guard an owner-library op: the granted token must carry `needed`
        scopes, else answer with the reconnect line (docs/SPOTIFY_NOTES.md —
        SCOPES changed, so an old connection must re-consent once)."""
        granted = set(str(self.tokens().get("scope") or "").split())
        if any(s not in granted for s in needed):
            raise SpotifyError(
                f"Spotify is connected with older permissions — reconnect "
                f"Spotify to allow {what}: Settings > Spotify > Disconnect, "
                "then Connect once")

    def _save_tokens(self, tokens: dict) -> None:
        self.store.set_json("spotify_tokens", tokens)

    # -- quota discipline ---------------------------------------------------
    def _quota_guard(self) -> None:
        left = self._quota_until - self._clock()
        if left > 0:
            raise SpotifyQuota(
                f"Spotify's dev-mode quota is still cooling down — wait "
                f"~{int(left) + 1}s before trying anything Spotify again "
                "(the hub holds off automatically; don't hammer it)",
                left)

    def _note_quota(self, retry_after: float) -> None:
        with self._lock:
            self._quota_until = self._clock() + retry_after
            self._quota_left = retry_after

    def quota_backoff(self) -> float:
        """Seconds left of the current back-off window (0 = clear)."""
        return max(0.0, self._quota_until - self._clock())

    def call(self, fn, *args, **kwargs):
        """Run one API op under the quota window; record 429s. Tool and route
        code goes through here so a hard quota answers instantly instead of
        hammering Spotify."""
        self._quota_guard()
        try:
            return fn(*args, **kwargs)
        except SpotifyQuota as q:
            self._note_quota(q.retry_after)
            raise

    # -- connect flow --------------------------------------------------------
    def connect_url(self, redirect_uri: str = "") -> dict:
        if not self.configured():
            raise SpotifyNotConfigured(
                "Spotify client id is not set — create the Developer "
                "Dashboard app (docs/SPOTIFY_NOTES.md) and save its Client ID "
                "under Settings > Spotify")
        redirect_uri = (str(redirect_uri or "").strip()
                        or DEFAULT_REDIRECT_URI)
        verifier, challenge = make_pkce()
        state = _b64url(secrets.token_bytes(12))
        self.store.set_json("spotify_pending", {
            "verifier": verifier, "redirect_uri": redirect_uri,
            "state": state, "created": time.time()})
        url = authorize_url(self.settings()["client_id"], redirect_uri,
                            state, challenge,
                            accounts_base=(str(self.settings().get("accounts_url") or "")
                                           .strip() or DEFAULT_ACCOUNTS_BASE))
        # The PKCE verifier stays on the hub: it is never sent to clients.
        return {"url": url, "state": state, "redirect_uri": redirect_uri,
                "expires_in": PENDING_TTL_S}

    def connect(self, payload: dict) -> dict:
        """Exchange a code (+ PKCE verifier) for tokens, or accept the full
        redirected URL pasted back. Either {code, redirect_uri, code_verifier?}
        or {paste}."""
        payload = payload or {}
        pending = self.store.get_json("spotify_pending", {}) or {}
        redirect_uri = str(payload.get("redirect_uri") or "").strip()
        code = str(payload.get("code") or "").strip()
        verifier = str(payload.get("code_verifier") or "").strip()
        state = str(payload.get("state") or "").strip()
        if not code and not verifier:
            paste = payload.get("paste")
            if paste is None:
                raise SpotifyError("send {code, redirect_uri, code_verifier} "
                                   "or {paste: <the redirected URL>}")
            parsed = parse_redirect_paste(str(paste))
            code = parsed["code"]
            state = state or str(parsed.get("state") or "")
            redirect_uri = redirect_uri or parsed["redirect_uri"]
        if not verifier:
            # Using the hub-held PKCE pair: the handshake must be fresh and,
            # when Spotify's redirect carried a state, it must be ours (stops
            # a stale or foreign authorization code being bound to this hub).
            if pending and time.time() - float(pending.get("created") or 0) > PENDING_TTL_S:
                self.store.del_json("spotify_pending")
                raise SpotifyError("that Connect attempt expired — press "
                                   "Connect again")
            if pending and state and state != pending.get("state"):
                raise SpotifyError("the sign-in reply doesn't match the "
                                   "Connect attempt in progress (state "
                                   "mismatch) — press Connect again")
        if not redirect_uri:
            redirect_uri = str(pending.get("redirect_uri") or "").strip()
        if not redirect_uri:
            raise SpotifyError("redirect_uri is missing — paste the WHOLE "
                               "redirected URL, or send the desktop "
                               "listener's redirect_uri")
        verifier = verifier or str(pending.get("verifier") or "")
        if not verifier:
            raise SpotifyError("code_verifier is missing — start Connect "
                               "from the Settings page or the desktop app so "
                               "the PKCE pair isn't lost")
        client = self.client()
        try:
            tokens = client.exchange_code(code, redirect_uri, verifier)
        finally:
            self.store.del_json("spotify_pending")
        tokens["connected_at"] = time.time()
        name = ""
        try:
            me = client.me()
            name = str(me.get("display_name") or me.get("id") or "")
        except SpotifyError:
            pass                      # the name is nice-to-have, not required
        if name:
            tokens["connected_as"] = name
        self._save_tokens(tokens)
        self._now_cache = None
        self._last_sig = None
        with self._lock:
            self._playlists_cache = (0.0, [])   # fresh consent → fresh library
        if self.on_change:
            self.on_change()
        return self.status()

    def disconnect(self) -> dict:
        gone = self.store.del_json("spotify_tokens")
        self.store.del_json("spotify_pending")
        self._now_cache = None
        self._last_sig = None
        self._wake.clear()
        with self._lock:
            self._devices_cache = (0.0, [])
            self._playlists_cache = (0.0, [])
        if gone and self.on_change:
            self.on_change()
        return self.status()

    def status(self) -> dict:
        settings = self.settings()
        tokens = self.tokens()
        return {
            "enabled": bool(settings.get("enabled")),
            "configured": bool(str(settings.get("client_id") or "").strip()),
            "connected": self.connected(),
            "device_name": str(settings.get("device_name")
                               or DEFAULT_DEVICE_NAME),
            "connected_as": str(tokens.get("connected_as") or ""),
            "scope": str(tokens.get("scope") or ""),
            "scopes": SCOPES,
            # required scopes the granted token lacks → owner must
            # Disconnect + Connect once (docs/SPOTIFY_NOTES.md)
            "missing_scopes": self.missing_scopes(),
            "default_redirect_uri": DEFAULT_REDIRECT_URI,
            "quota_backoff_left": round(self.quota_backoff(), 1),
            # write-only tokens are NEVER included: only booleans/expiry
            "token_expires_at": float(tokens.get("expires_at") or 0) or None,
            "now_playing": self._now_cache,
            "polling": bool(self._wake.is_set()),
            # slow idle watch: period (0 = off) + whether it is live now
            "idle_poll_s": self.idle_poll_s(),
            "idle_watch": bool(self.idle_poll_s() > 0 and self.interested()),
        }

    # -- devices / playback ---------------------------------------------------
    def devices(self, force: bool = False) -> list[dict]:
        self.require()
        now = time.monotonic()
        with self._lock:
            stamp, cached = self._devices_cache
        if cached and not force and now - stamp < DEVICE_CACHE_S:
            return [dict(d) for d in cached]
        raw = self.call(self.client().player_devices)
        out = [{"id": str(d.get("id") or ""),
                "name": str(d.get("name") or ""),
                "type": str(d.get("type") or "Unknown"),
                "active": bool(d.get("is_active")),
                "volume": d.get("volume_percent")} for d in raw]
        with self._lock:
            self._devices_cache = (now, [dict(d) for d in out])
        return out

    def resolve_device(self, name_or_id: str = "") -> dict:
        """Resolve a device by id, exact name, or unique substring; '' = the
        settings player (device_name, default 'Riko'), else any active device."""
        devices = self.devices(force=True)
        needle = str(name_or_id or "").strip().lower()
        if not needle:
            needle = str(self.settings().get("device_name")
                         or DEFAULT_DEVICE_NAME).strip().lower()
        for d in devices:
            if d["id"] == name_or_id or d["name"].lower() == needle:
                return d
        hits = [d for d in devices if needle in d["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        named = ", ".join(sorted(d["name"] for d in devices)) or "none online"
        if len(hits) > 1:
            raise SpotifyError(f"device “{name_or_id}” matches several "
                               f"devices: {named}")
        raise SpotifyError(f"no Spotify Connect device named “{needle}” is "
                           f"online right now — online: {named}. On the "
                           f"desktop, start the player from the tray: Music "
                           f"> Start player")

    def playlists(self, force: bool = False) -> list[dict]:
        """The owner's own playlists [{name, uri, tracks}] — GET
        /me/playlists (limit 50 + offset, inside the quota window) cached
        ~5 min; invalidated by connect/disconnect. needs
        playlist-read-private/collaborative."""
        self.require()
        self.require_scope(PLAYLIST_SCOPES, "playlists")
        now = time.monotonic()
        with self._lock:
            stamp, cached = self._playlists_cache
        if cached and not force and now - stamp < PLAYLIST_CACHE_S:
            return [dict(p) for p in cached]
        raw = self.call(self.client().my_playlists)
        out = [{"name": str(p.get("name") or ""),
                "uri": str(p.get("uri") or ""),
                "tracks": (p.get("tracks") or {}).get("total")
                if isinstance(p.get("tracks"), dict) else None}
               for p in raw if p]
        with self._lock:
            self._playlists_cache = (now, [dict(p) for p in out])
        return out

    def _playback_device(self, wanted: dict) -> str:
        """Return a device id to send play commands to, transferring first
        when the wanted device isn't the active one."""
        devices = self.devices()
        active = next((d for d in devices if d["active"]), None)
        if active and active["id"] == wanted["id"]:
            return wanted["id"]
        self.call(self.client().transfer, wanted["id"], False)
        return wanted["id"]

    def play(self, query: str, kind: str = "track",
             device: str = "", shuffle: bool = False) -> dict:
        """query → what to play. The OWNER's library is searched first:
        'my liked/saved songs' plays the saved tracks (GET /me/tracks →
        uris — the Web API can't start the Liked Songs context directly),
        and a playlist query is matched against the owner's own playlists
        first (catalogue /search does NOT reliably surface the user's own,
        esp. private, playlists). Otherwise: catalogue search (limit ≤ 10,
        null entries filtered) → top hit → start it on the chosen device.
        A query mentioning 'playlist' forces kind=playlist ('play my X
        playlist' arrives as track otherwise). Returns {uri, name, artists,
        device, kind, source, alternatives}."""
        self.require()
        query = str(query or "").strip()
        if not query:
            raise SpotifyError("say what to play (a song, artist, album or "
                               "playlist)")
        kind = str(kind or "track").strip().lower()
        if kind not in ("track", "album", "playlist", "artist"):
            raise SpotifyError("kind must be track, album, playlist or artist")
        if "playlist" in query.lower():
            kind = "playlist"        # 'play my dance playlist' says track
        if kind != "playlist" and is_liked_songs(query):
            return self.play_liked_songs(device=device, shuffle=shuffle)
        if kind == "playlist":
            best, alternatives = match_playlists(query, self.playlists())
            if best:
                wanted = self.resolve_device(device)
                dev_id = self._playback_device(wanted)
                self.call(self.client().start_playback,
                          context_uri=best["uri"], device_id=dev_id)
                self.wake()
                return {"uri": best["uri"], "name": best["name"],
                        "artists": "", "device": wanted["name"],
                        "kind": "playlist", "source": "library",
                        "alternatives": alternatives}
        artists = ""
        items = self.call(self.client().search, query, kind, 3)
        if kind == "artist":
            # artists are not directly playable: top hit becomes their
            # top artist playlist (a playable context)
            if not items:
                raise SpotifyError(f"Spotify found no artist matching “{query}”")
            uri = str(items[0].get("uri") or "")
            name = str(items[0].get("name") or query)
            artist_id = uri.split(":")[-1] if uri.count(":") == 2 else ""
            if artist_id:
                pl = self.client().search(
                    name, "playlist", 1)  # heuristic: an artist playlist
                if pl:
                    uri = str(pl[0].get("uri") or uri)
        else:
            if not items:
                if kind == "playlist":
                    raise SpotifyError(
                        f"Spotify found no playlist matching “{query}” — "
                        "not in your own playlists and not in search "
                        "(spotify_playlists lists what you have)")
                raise SpotifyError(f"Spotify found no {kind} matching “{query}”")
            top = items[0]
            uri = str(top.get("uri") or "")
            if kind == "track":
                name = str(top.get("name") or query)
                artists = ", ".join(str(a.get("name") or "")
                                    for a in (top.get("artists") or []) if a.get("name"))
            else:
                name = str(top.get("name") or query)
                artists = ""
        if not uri:
            raise SpotifyError("Spotify returned no playable uri")
        wanted = self.resolve_device(device)
        dev_id = self._playback_device(wanted)
        if kind == "track" and uri.startswith("spotify:track:"):
            self.call(self.client().start_playback, uris=[uri],
                      device_id=dev_id)
        else:
            self.call(self.client().start_playback, context_uri=uri,
                      device_id=dev_id)
        self.wake()
        return {"uri": uri, "name": name, "artists": artists,
                "device": wanted["name"], "kind": kind, "source": "search",
                "alternatives": []}

    def play_liked_songs(self, device: str = "",
                         shuffle: bool = False) -> dict:
        """'my liked/saved songs': first page of GET /me/tracks (≤ 50) →
        start_playback with the uris (the Web API can't start the Liked
        Songs context directly). shuffle=True shuffles the uris first.
        needs user-library-read."""
        self.require()
        self.require_scope(LIKED_SCOPES, "liked songs")
        uris = self.call(self.client().saved_tracks)
        if not uris:
            raise SpotifyError("your Spotify Liked Songs list is empty — "
                               "like a track first (spotify_like_current)")
        if shuffle:
            random.shuffle(uris)
        wanted = self.resolve_device(device)
        dev_id = self._playback_device(wanted)
        self.call(self.client().start_playback, uris=uris, device_id=dev_id)
        self.wake()
        return {"uri": "", "name": "Liked Songs", "artists": "",
                "device": wanted["name"], "kind": "liked",
                "source": "library", "count": len(uris),
                "shuffle": bool(shuffle), "alternatives": []}

    def control(self, action: str, value=None) -> dict:
        """pause|resume|next|prev|seek|volume|shuffle|toggle. seek/volume take
        a value (seconds / percent); shuffle a bool-ish. 'toggle' (the ticker
        button, docs/SPOTIFY_NOTES.md) reads the current snapshot and flips."""
        self.require()
        action = str(action or "").strip().lower()
        c = self.client()
        if action == "toggle":
            # fresh-enough truth for the flip: the young cache, else one call
            snap = self._now_cache
            if snap is None or time.monotonic() - float(
                    snap.get("_fetched") or 0) > 5.0:
                snap = self._fetch_snapshot()
            return self.control("pause" if snap.get("playing") else "resume",
                                value)
        if action == "pause":
            self.call(c.pause)
            self._wake.clear()
            return {"action": "pause"}
        if action in ("resume", "play"):
            self.call(c.start_playback)
            self.wake()
            return {"action": "resume"}
        if action in ("next", "skip"):
            self.call(c.next_track)
            self.wake()
            return {"action": "next"}
        if action in ("prev", "previous", "back"):
            self.call(c.previous_track)
            self.wake()
            return {"action": "prev"}
        if action == "seek":
            if value is None:
                raise SpotifyError("seek needs a value: the position in "
                                   "seconds from the start of the track")
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                raise SpotifyError("seek needs a value: the position in "
                                   "seconds from the start of the track")
            if seconds < 0:
                raise SpotifyError("seek position can't be negative")
            self.call(c.seek, int(seconds * 1000))
            return {"action": "seek", "position_ms": int(seconds * 1000)}
        if action == "volume":
            if value is None:
                raise SpotifyError("volume needs a value 0-100")
            try:
                percent = int(round(float(value)))
            except (TypeError, ValueError):
                raise SpotifyError("volume needs a value 0-100")
            if not 0 <= percent <= 100:
                raise SpotifyError("volume must be between 0 and 100")
            self.call(c.set_volume, percent)
            return {"action": "volume", "volume_percent": percent}
        if action == "shuffle":
            if isinstance(value, str):
                on = value.strip().lower() in ("1", "true", "on", "yes")
            else:
                on = bool(value)
            self.call(c.set_shuffle, on)
            return {"action": "shuffle", "shuffle": on}
        raise SpotifyError("action must be pause, resume, next, prev, seek, "
                           "volume or shuffle")

    def transfer(self, device: str) -> dict:
        self.require()
        wanted = self.resolve_device(device)
        self.call(self.client().transfer, wanted["id"], True)
        self.wake()
        return {"device": wanted["name"], "id": wanted["id"]}

    def now_playing(self, force: bool = False) -> dict:
        """Fresh-enough snapshot for late clients: the cache when young, else
        one API call (also refreshes the cache + wakes the poller if playing).
        The call itself is a page-interest signal: a ticker page that asks is
        a page that wants the idle watch (see `interested`)."""
        self._np_requested = self._clock()
        self.require()
        if not force and self._now_cache is not None and \
                time.monotonic() - float(self._now_cache.get("_fetched") or 0) < 5.0:
            out = dict(self._now_cache)
            out.pop("_fetched", None)
            return out
        snap = self._fetch_snapshot()
        if snap.get("playing"):
            self.wake()
        return snap

    def _fetch_snapshot(self) -> dict:
        cp = self.call(self.client().current_playback)
        snap = track_snapshot(cp)
        snap["_fetched"] = time.monotonic()
        self._now_cache = dict(snap)
        self.store.set_json("spotify_now", {k: v for k, v in snap.items()
                                            if not k.startswith("_")})
        return {k: v for k, v in snap.items() if not k.startswith("_")}

    def like_current(self) -> dict:
        self.require()
        snap = self._fetch_snapshot()
        uri = snap.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            raise SpotifyError("nothing is playing right now — there is "
                               "nothing to like")
        self.call(self.client().add_to_library, [uri])
        return {"uri": uri, "track": snap.get("track", ""),
                "artists": snap.get("artists", "")}

    # -- now_playing poller ---------------------------------------------------
    def wake(self) -> None:
        """Something may be playing now (play/resume/transfer/connect, or a
        desktop Soloist nudge): switch back to the FAST poll."""
        self._wake.set()

    def idle_poll_s(self) -> float:
        """Slow-watch period from settings `spotify.idle_poll_s`:
        0 = disabled; anything else floors at MIN_IDLE_POLL_S (quota
        discipline — this watch exists to be cheap, not fast)."""
        try:
            v = float(self.settings().get("idle_poll_s", IDLE_POLL_S))
        except (TypeError, ValueError):
            v = IDLE_POLL_S
        if v <= 0:
            return 0.0
        return max(MIN_IDLE_POLL_S, v)

    def interested(self) -> bool:
        """Does anyone care about a now_playing change right now? Either a
        desktop client is online (interest_hook, wired to clients_hub by
        hub/server.py) or some client fetched GET /api/spotify/now-playing
        within the last PAGE_INTEREST_S — the ticker's seed fetch happens
        when a page opens, so a recent ask means a ticker page is up."""
        if self._clock() - self._np_requested < PAGE_INTEREST_S:
            return True
        try:
            return bool(self.interest_hook and self.interest_hook())
        except Exception:
            return False

    def _publish_change(self, snap: dict) -> None:
        """Publish now_playing ONLY on a real state change (dedupe):
        (uri, playing, device) is the signature the ticker flips on."""
        sig = (snap.get("uri", ""), snap.get("playing", False),
               snap.get("device", ""))
        if sig == self._last_sig:
            return
        self._last_sig = sig
        payload = {k: v for k, v in snap.items() if not k.startswith("_")}
        self.events.publish("now_playing", **payload)

    def startup_snapshot(self) -> dict | None:
        """One immediate snapshot when the poller starts (if connected):
        playback that began while the hub was down must be reflected right
        away, not one idle period later. Also the hub-restart answer to
        'is anything playing?' — the cache and (if playing) the fast poll
        come up correct."""
        if not self.connected() or not self.configured() or not self.enabled():
            return None
        if self.quota_backoff() > 0:
            return None                # cooling down: the idle watch retries
        try:
            snap = self._fetch_snapshot()
        except SpotifyError:
            return None                # auth/network: nothing to seed from
        self._publish_change(snap)
        if snap.get("playing"):
            self._wake.set()
        return snap

    def idle_tick(self) -> dict | None:
        """One SLOW-watch cycle (public so tests drive it directly): runs
        only while NOT fast-polling, the watch is enabled
        (idle_poll_s > 0), someone is interested, and no quota back-off is
        cooling. One currently-playing fetch; publishes on change; a
        play/resume noticed here hands over to the fast poll (wake). A
        fetch failure does NOT switch the watch off — the next period
        tries again (unlike the fast poll, the watch has no flag to lose)."""
        if self._wake.is_set():
            return None                # the fast poll owns this moment
        if not self.connected() or not self.configured() or not self.enabled():
            return None
        if self.idle_poll_s() <= 0 or not self.interested():
            return None                # disabled, or nobody watching
        if self.quota_backoff() > 0:
            return None                # respect the back-off window
        try:
            snap = self._fetch_snapshot()
        except SpotifyError:
            return None                # a poll hiccup must never spam events
        self._publish_change(snap)
        if snap.get("playing"):
            self._wake.set()           # playing now: POLL_S cadence from here
        return snap

    def poll_tick(self) -> dict | None:
        """One FAST poll cycle (public so tests drive it directly). The wake
        flag IS the fast-poll throttle: no flag → NO fast API calls
        (the idle watch is a separate, slower budget: `idle_tick`).
        Publishes now_playing ONLY on a real state change (dedupe), and
        clears the flag when state is paused/idle — the loop then rests on
        the idle watch."""
        if not self.connected() or not self.configured() or not self.enabled():
            self._wake.clear()
            return None
        if not self._wake.is_set():
            return None                # paused / idle: never hammer the API
        if self.quota_backoff() > 0:
            return None
        try:
            snap = self._fetch_snapshot()
        except SpotifyError:
            self._wake.clear()         # auth died: stop until the next play
            return None            # a poll hiccup must never spam events
        self._publish_change(snap)
        if not snap.get("playing"):
            self._wake.clear()     # paused/idle: fall back to the slow watch
        return snap

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()        # a restarted poller must not inherit stop
            self._thread = threading.Thread(target=self._loop,
                                            name="spotify-poller", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        # hub start = one immediate snapshot: state after a restart is
        # correct even when playback began while the hub was down
        try:
            self.startup_snapshot()
        except Exception:
            pass
        while not self._stop.is_set():
            if self._wake.is_set():
                # poll_tick owns the flag: clears it on pause/idle/auth
                # death, keeps it set while playing (next tick after POLL_S).
                try:
                    self.poll_tick()
                except Exception:
                    pass
                self._stop.wait(self.poll_s)
                continue
            # not playing: the SLOW idle watch (external play/resume is
            # noticed within one idle_poll_s); a wake() cuts it short at
            # once — a hub-initiated play must hit the API immediately.
            try:
                self.idle_tick()
            except Exception:
                pass
            period = self.idle_poll_s()
            self._wake.wait(period if period > 0 else 1.0)
