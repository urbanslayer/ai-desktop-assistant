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
most every POLL_S (5 s); the poller sleeps with NO API calls the moment
state is paused/idle and wakes on the next play/resume/transfer.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse

import requests

DEFAULT_ACCOUNTS_BASE = "https://accounts.spotify.com"
DEFAULT_API_BASE = "https://api.spotify.com/v1"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8911/callback"
DEFAULT_DEVICE_NAME = "Riko"
# scopes the tools need: player state + control, currently-playing for the
# event/late clients, and library writes for spotify_like_current
SCOPES = ("user-read-playback-state user-modify-playback-state "
          "user-read-currently-playing user-library-modify")
SEARCH_LIMIT_MAX = 10          # dev-mode hard cap (docs/SPOTIFY_NOTES.md)
POLL_S = 5.0                   # now_playing poll ceiling while playing
MIN_POLL_S = 1.0               # test floor
QUOTA_FALLBACK_S = 30.0        # 429 without a usable Retry-After
REFRESH_SLACK_S = 60.0         # refresh this long before expiry
DEVICE_CACHE_S = 60.0          # device list cache (quota discipline)
USER_AGENT = "riko-hub/1 spotify"
PENDING_TTL_S = 900.0          # a PKCE handshake is single-use and short


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
        SpotifyPremium; other non-2xx → SpotifyError."""
        url = self.api_base.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.access_token()}",
                   "User-Agent": USER_AGENT}
        r = self._send(method, url, headers, params, body)
        if r.status_code == 401:
            # a rotated/revoked token: refresh once, retry once
            headers["Authorization"] = f"Bearer {self.refresh()}"
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
        Returns the raw items (already the right type for `kind`)."""
        limit = max(1, min(SEARCH_LIMIT_MAX, int(limit)))
        data = self.api("GET", "/search",
                        params={"q": query, "type": kind, "limit": limit})
        return ((data or {}).get(kind) or {}).get("items") or []

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
    and the event publishing. The routes and the tools are thin shells."""

    def __init__(self, store, events, settings_fn, *, defaults: dict | None = None,
                 on_change=None):
        self.store = store
        self.events = events
        self.settings_fn = settings_fn      # -> merged spotify settings dict
        self.on_change = on_change          # bump_state hook (connect/disconnect)
        d = dict(defaults or {})
        self.poll_s = max(MIN_POLL_S, float(d.get("poll_s") or POLL_S))
        self._quota_until = 0.0             # monotonic deadline after a 429
        self._quota_left = 0.0
        self._lock = threading.Lock()
        self._devices_cache: tuple[float, list] = (0.0, [])
        self._now_cache: dict | None = None
        self._last_sig: tuple | None = None
        self._wake = threading.Event()      # set = poll (something is playing)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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

    def _save_tokens(self, tokens: dict) -> None:
        self.store.set_json("spotify_tokens", tokens)

    # -- quota discipline ---------------------------------------------------
    def _quota_guard(self) -> None:
        left = self._quota_until - time.monotonic()
        if left > 0:
            raise SpotifyQuota(
                f"Spotify's dev-mode quota is still cooling down — wait "
                f"~{int(left) + 1}s before trying anything Spotify again "
                "(the hub holds off automatically; don't hammer it)",
                left)

    def _note_quota(self, retry_after: float) -> None:
        with self._lock:
            self._quota_until = time.monotonic() + retry_after
            self._quota_left = retry_after

    def quota_backoff(self) -> float:
        """Seconds left of the current back-off window (0 = clear)."""
        return max(0.0, self._quota_until - time.monotonic())

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
        return {"url": url, "state": state, "redirect_uri": redirect_uri,
                "code_verifier": verifier, "expires_in": PENDING_TTL_S}

    def connect(self, payload: dict) -> dict:
        """Exchange a code (+ PKCE verifier) for tokens, or accept the full
        redirected URL pasted back. Either {code, redirect_uri, code_verifier?}
        or {paste}."""
        payload = payload or {}
        pending = self.store.get_json("spotify_pending", {}) or {}
        redirect_uri = str(payload.get("redirect_uri") or "").strip()
        code = str(payload.get("code") or "").strip()
        verifier = str(payload.get("code_verifier") or "").strip()
        if not code and not verifier:
            paste = payload.get("paste")
            if paste is None:
                raise SpotifyError("send {code, redirect_uri, code_verifier} "
                                   "or {paste: <the redirected URL>}")
            parsed = parse_redirect_paste(str(paste))
            code = parsed["code"]
            redirect_uri = redirect_uri or parsed["redirect_uri"]
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
            "default_redirect_uri": DEFAULT_REDIRECT_URI,
            "quota_backoff_left": round(self.quota_backoff(), 1),
            # write-only tokens are NEVER included: only booleans/expiry
            "token_expires_at": float(tokens.get("expires_at") or 0) or None,
            "now_playing": self._now_cache,
            "polling": bool(self._wake.is_set()),
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
             device: str = "") -> dict:
        """query → search (limit ≤ 10) → top hit → start it on the chosen
        device. Returns {uri, name, artists, device, kind}."""
        self.require()
        query = str(query or "").strip()
        if not query:
            raise SpotifyError("say what to play (a song, artist, album or "
                               "playlist)")
        kind = str(kind or "track").strip().lower()
        if kind not in ("track", "album", "playlist", "artist"):
            raise SpotifyError("kind must be track, album, playlist or artist")
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
                "device": wanted["name"], "kind": kind}

    def control(self, action: str, value=None) -> dict:
        """pause|resume|next|prev|seek|volume|shuffle. seek/volume take a
        value (seconds / percent); shuffle a bool-ish."""
        self.require()
        action = str(action or "").strip().lower()
        c = self.client()
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
        one API call (also refreshes the cache + wakes the poller if playing)."""
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
        """Something may be playing now (play/resume/transfer/connect):
        start polling again."""
        self._wake.set()

    def poll_tick(self) -> dict | None:
        """One poll cycle (public so tests drive it directly). The wake flag
        IS the throttle: no flag → NO API calls at all (paused/idle).
        Publishes now_playing ONLY on a real state change (dedupe), and
        clears the flag when state is paused/idle — the loop then rests."""
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
        sig = (snap.get("uri", ""), snap.get("playing", False),
               snap.get("device", ""))
        if sig != self._last_sig:
            self._last_sig = sig
            payload = {k: v for k, v in snap.items() if not k.startswith("_")}
            self.events.publish("now_playing", **payload)
        if not snap.get("playing"):
            self._wake.clear()     # paused/idle: stop polling entirely
        return snap

    def start(self) -> None:
        if self._thread is None:
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
        while not self._stop.is_set():
            if not self._wake.is_set():
                # paused / idle / disconnected: NO Spotify calls at all
                self._wake.wait(1.0)
                continue
            # poll_tick owns the flag: clears it on pause/idle/auth death,
            # keeps it set while playing (next tick after the gap).
            try:
                snap = self.poll_tick()
            except Exception:
                snap = None
            self._stop.wait(self.poll_s)
