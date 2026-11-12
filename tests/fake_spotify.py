"""In-repo fake Spotify (music phase 1 tests): ONE loopback HTTP server that
plays both roles — accounts.spotify.com (POST /api/token) and
api.spotify.com/v1 (search, player, devices, library, me, me/playlists,
me/tracks). Tests point the hub's spotify.accounts_url / spotify.api_url
settings at it; no real network is ever touched.

Programmable per endpoint: `one_shot(method, path, status, body, headers)`
queues a single canned response (e.g. 429 QUOTA_EXCEEDED with Retry-After),
everything else follows the state fields (.tokens, .now, .devices, .search).
Every request is recorded in .log as {method, path, query, form, json, auth}
— tests assert against that instead of guessing.
"""
from __future__ import annotations

import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRACK = {"uri": "spotify:track:AAA", "name": "Bohemian Rhapsody",
         "duration_ms": 354000,
         "artists": [{"name": "Queen"}],
         "album": {"name": "A Night at the Opera", "images": [
             {"url": "https://i.scdn.co/image/big", "width": 640,
              "height": 640},
             {"url": "https://i.scdn.co/image/small", "width": 64,
              "height": 64}]}}
# The owner's live query 'hide && seek by i-dle': punctuation in the title,
# '(G)I-DLE' spelled with parens on Spotify's side. Real-shape fixture so
# the real matching rules are what get exercised (bug 2026-11-12: singular
# response keys made EVERY catalogue search return []).
HIDE = {"uri": "spotify:track:HIDE1", "name": "HIDE AND SEEK",
        "duration_ms": 180000,
        "artists": [{"name": "(G)I-DLE"}],
        "album": {"name": "I feel", "images": [
            {"url": "https://i.scdn.co/image/hide", "width": 640,
             "height": 640}]}}
DEVICE_RIKO = {"id": "dev-riko", "name": "Riko", "type": "Speaker",
               "is_active": True, "volume_percent": 40}
DEVICE_ECHO = {"id": "dev-echo", "name": "Echo Kitchen", "type": "Smartphone",
               "is_active": False, "volume_percent": 60}

# GET /v1/search answers under PLURAL type keys — the real API shape
# ({'tracks': {'items': [...]}}); the fake used the singular kind and hid
# a production bug that returned [] for EVERY search (2026-11-12).
SEARCH_PLURAL = {"track": "tracks", "album": "albums",
                 "artist": "artists", "playlist": "playlists"}


def _compact(word) -> str:
    """Search-match key: casefold + drop everything non-alphanumeric
    ('(G)I-DLE' -> 'gidle', 'i-dle' -> 'idle', 'hide && seek' words ->
    'hide','seek')."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(word or "")).casefold()
    return "".join(c for c in s if c.isalnum())


def _words(text) -> list:
    return [w for w in (_compact(t) for t in str(text or "").split()) if w]


def _word_hit(word: str, words: list) -> bool:
    # 'i-dle' must hit '(G)I-DLE': after compaction, containment either way
    return any(w == word or word in w or w in word for w in words)


def search_matches(q: str, item: dict) -> bool:
    """Crude Spotify-like matcher: q may carry field filters
    (track:"…" artist:"…" album:"…"); leftover plain words must hit the
    name or any artist name."""
    import re
    fields = dict(re.findall(r'(track|artist|album):"([^"]*)"', q or ""))
    rest = re.sub(r'(track|artist|album):"[^"]*"', " ", q or "")
    name_words = _words(item.get("name"))
    artist_words = [w for a in (item.get("artists") or [])
                    if isinstance(a, dict) for w in _words(a.get("name"))]
    if fields.get("track") and not all(
            _word_hit(w, name_words) for w in _words(fields["track"])):
        return False
    if fields.get("album") and not all(
            _word_hit(w, _words((item.get("album") or {}).get("name")))
            for w in _words(fields["album"])):
        return False
    if fields.get("artist") and not all(
            _word_hit(w, artist_words) for w in _words(fields["artist"])):
        return False
    for w in _words(rest):
        if not _word_hit(w, name_words) and not _word_hit(w, artist_words):
            return False
    return True


class _Handler(BaseHTTPRequestHandler):
    def _owner(self):
        return self.server.fake

    def _record(self, body_raw):
        fake = self._owner()
        parsed = urllib.parse.urlsplit(self.path)
        entry = {"method": self.command, "path": parsed.path,
                 "query": dict(urllib.parse.parse_qsl(parsed.query)),
                 "form": {}, "json": None,
                 "auth": self.headers.get("Authorization", "")}
        if body_raw:
            if "application/x-www-form-urlencoded" in (
                    self.headers.get("Content-Type") or ""):
                entry["form"] = dict(urllib.parse.parse_qsl(body_raw.decode()))
            else:
                try:
                    import json
                    entry["json"] = json.loads(body_raw.decode())
                except ValueError:
                    pass
        fake.log.append(entry)
        return parsed

    def _respond(self, status, body, headers=None):
        payload = b"" if body is None else (
            body if isinstance(body, bytes)
            else _json_bytes(body))
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _dispatch(self, body_raw):
        fake = self._owner()
        parsed = self._record(body_raw)
        shot = fake.take_shot(self.command, parsed.path)
        if shot is not None:
            status, body, headers = shot
            return self._respond(status, body, headers)
        return fake.route(self.command, parsed.path,
                          fake.log[-1]["query"],
                          body_raw, self._respond)

    def do_GET(self):
        self._dispatch(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._dispatch(self.rfile.read(length) if length else b"")

    do_PUT = do_POST
    do_DELETE = do_POST

    def log_message(self, format, *args):
        pass


def _json_bytes(obj) -> bytes:
    import json
    return json.dumps(obj).encode("utf-8")


class FakeSpotify:
    """The programmable state + the server. `base` is the root URL; the hub
    settings are accounts_url=base, api_url=base+'/v1'."""

    def __init__(self):
        self.log: list[dict] = []
        self._shots: dict[tuple[str, str], list] = {}
        self.tokens = {"access_token": "ACCESS-1", "refresh_token": "REFRESH-1",
                       "expires_in": 3600, "token_type": "Bearer",
                       "scope": "user-read-playback-state"}
        self.token_error = None            # (status, body) for /api/token
        self.search = {"track": [TRACK, HIDE], "playlist": [], "album": [],
                       "artist": [{"uri": "spotify:artist:ART1",
                                   "name": "Queen"},
                                  {"uri": "spotify:artist:GIDLE",
                                   "name": "(G)I-DLE"}]}
        # owner library (2026-11-10 round): /me/playlists + /me/tracks.
        # Tests fill these; None entries mimic Spotify's interleaved nulls.
        self.playlists = []                # [{uri, name, tracks:{total}}]
        self.saved = []                    # [{"track": {...}} | None]
        self.now = None                    # currently-playing dict | None=204
        self.devices = [dict(DEVICE_RIKO), dict(DEVICE_ECHO)]
        self.me = {"id": "owner", "display_name": "Owner Person"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.fake = self
        self.server.daemon_threads = True
        import threading
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- programmability ---------------------------------------------------
    def one_shot(self, method: str, path: str, status: int, body=None,
                 headers=None) -> None:
        """Queue ONE canned response for method+path (before state routing)."""
        self._shots.setdefault((method.upper(), path), []).append(
            (status, body, headers or {}))

    def take_shot(self, method, path):
        queue = self._shots.get((method, path))
        return queue.pop(0) if queue else None

    def hits(self, method: str, path: str) -> int:
        return sum(1 for e in self.log
                   if e["method"] == method and e["path"] == path)

    def last(self, method: str, path: str) -> dict:
        for e in reversed(self.log):
            if e["method"] == method and e["path"] == path:
                return e
        return {}

    # -- routing ------------------------------------------------------------
    def route(self, method, path, query, body_raw, respond):
        if path == "/api/token":
            if self.token_error:
                return respond(*self.token_error)
            entry = self.log[-1]
            form = entry["form"]
            if form.get("grant_type") == "authorization_code":
                if not form.get("code") or not form.get("code_verifier"):
                    return respond(400, {"error": "invalid_request",
                                         "error_description": "missing code/verifier"})
                body = dict(self.tokens)
                body["access_token"] = "ACCESS-EXCHANGED"
                return respond(200, body)
            # refresh
            body = dict(self.tokens)
            body["access_token"] = "ACCESS-REFRESHED"
            body.pop("refresh_token", None)     # Spotify often omits it
            return respond(200, body)
        if method == "GET" and path == "/v1/me":
            return respond(200, self.me)
        if method == "GET" and path == "/v1/search":
            kind = query.get("type", "track")
            q = query.get("q") or ""
            pool = [i for i in self.search.get(kind, []) if i]
            hits = [i for i in pool if search_matches(q, i)]
            import re as _re
            filtered = bool(_re.search(r'(track|artist|album):"', q))
            # Field-filtered queries are honoured strictly (a real Spotify
            # answers exactly nothing when track:/artist: don't match).
            # Plain queries are forgiving like Spotify's: a query matching
            # nothing in this tiny pool hands back the pool. Null entries
            # interleave like the real API's.
            items = (hits if filtered else (hits or pool)) + [None]
            return respond(200, {SEARCH_PLURAL.get(kind, kind + "s"):
                                 {"items": items}})
        if method == "GET" and path.startswith("/v1/") and path.count("/") == 3:
            kind, _, ident = path[len("/v1/"):].partition("/")
            if kind in ("tracks", "albums", "artists", "playlists"):
                # single-item GETs (name resolution for direct URI play)
                for i in self.search.get(kind.rstrip("s"), []):
                    if i and str(i.get("uri") or "").endswith(":" + ident):
                        return respond(200, i)
                return respond(200, {"uri": f"spotify:{kind[:-1]}:{ident}",
                                     "name": f"Fake {kind[:-1]} {ident}"})
        if method == "GET" and path == "/v1/me/playlists":
            limit = int(query.get("limit") or 50)
            offset = int(query.get("offset") or 0)
            page = self.playlists[offset:offset + limit]
            nxt = (f"{self.base}/v1/me/playlists?limit={limit}"
                   f"&offset={offset + limit}"
                   if offset + limit < len(self.playlists) else None)
            return respond(200, {"items": page, "next": nxt})
        if method == "GET" and path == "/v1/me/tracks":
            limit = int(query.get("limit") or 50)
            return respond(200, {"items": self.saved[:limit]})
        if method == "GET" and path == "/v1/me/player/currently-playing":
            if self.now is None:
                return respond(204, None)
            return respond(200, self.now)
        if method == "GET" and path == "/v1/me/player/devices":
            return respond(200, {"devices": self.devices})
        if path.startswith("/v1/me/player") or path == "/v1/me/library":
            return respond(202, None)
        return respond(404, {"error": {"message": f"fake: no route {method} {path}"}})


def entry_auth_ok(*a):
    return True


def playing_snapshot(uri=TRACK["uri"], name=TRACK["name"], is_playing=True,
                     device=DEVICE_RIKO, progress_ms=1000):
    """A currently-playing body shaped like Spotify's."""
    return {"is_playing": bool(is_playing), "progress_ms": progress_ms,
            "item": dict(TRACK, uri=uri, name=name),
            "device": dict(device),
            "context": {"uri": uri}}
