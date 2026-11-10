"""The desktop's local OAuth listener for Spotify (music phase 1).

docs/SPOTIFY_NOTES.md: Spotify redirect URIs must be HTTPS or a 127.0.0.1
literal — the hub on the LAN fails that rule, so the DESKTOP receives the
browser redirect at http://127.0.0.1:8911/callback (fixed port, config
override desktop.music.oauth_port), and hands {code, state, redirect_uri}
to the hub, which holds the PKCE pair and exchanges the token.

The tray's Music > Connect Spotify does: GET /api/spotify/connect-url from
the hub → open the browser → this listener catches the redirect → POSTs it
to /api/spotify/connect → answers the tab with "you can close this now".
"""
from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8911
CALLBACK_PATH = "/callback"

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Riko \u2014 Spotify</title>
<style>body{{font-family:sans-serif;background:#14151a;color:#e8e8ee;display:grid;
place-items:center;height:100vh;margin:0}}main{{text-align:center;padding:2rem}}
h1{{font-size:1.4rem;margin:0 0 .6rem}}p{{color:#aab;line-height:1.5}}</style></head>
<body><main><h1>{mark} {title}</h1><p>{body}</p></main></body></html>"""


def _result_page(ok: bool, detail: str = "") -> str:
    if ok:
        return _PAGE.format(mark="\u2705", title="Spotify connected",
                            body="Riko has the token now — you can close "
                                 "this tab.")
    return _PAGE.format(mark="\u26a0\ufe0f", title="Spotify login failed",
                        body=f"The hub refused the login handoff: {detail}. "
                             "Try Music > Connect Spotify again.")


class OAuthListener:
    """Tiny loopback HTTP server (daemon thread). start() is idempotent;
    a port already bound is NOT fatal (another listener/desktop instance
    owns it — the tray action still opens the browser)."""

    def __init__(self, hub, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 log=print):
        self.hub = hub                       # riko.hub_client.HubClient (or fake)
        self.host = str(host or DEFAULT_HOST)
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT
        self.log = log
        self._server = None
        self._thread = None
        self.last_error = ""

    # -- the flow ----------------------------------------------------------
    def connect_url(self) -> dict:
        """Ask the hub to start the handshake: {url, state, ...}."""
        return self.hub._json("GET", "/api/spotify/connect-url")

    def open_browser(self, url: str) -> None:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:                     # headless: show the URL
            self.log(f"[music] open this URL to connect Spotify: {url} ({e})")

    def connect(self) -> dict:
        """Tray action: make sure we're listening, fetch the handshake URL,
        open the browser. The callback finishes the dance."""
        self.start()
        info = self.connect_url()
        self.open_browser(str(info.get("url") or ""))
        return info

    def handle_callback(self, query: str) -> tuple[bool, str]:
        """Forward {code, state, redirect_uri} to the hub. Returns
        (ok, detail) for the browser tab."""
        qs = urllib.parse.parse_qs(query)
        payload = {
            "code": (qs.get("code") or [""])[0],
            "state": (qs.get("state") or [""])[0],
            "redirect_uri": f"http://{self.host}:{self.port}{CALLBACK_PATH}",
        }
        if not payload["code"]:
            err = (qs.get("error") or ["no code"])[0]
            desc = (qs.get("error_description") or [err])[0]
            self.last_error = desc
            return False, desc
        try:
            self.hub._json("POST", "/api/spotify/connect", json=payload)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)[:300]
        return True, ""

    # -- server lifecycle ----------------------------------------------------
    def start(self) -> bool:
        if self._server is not None:
            return True
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                ok, detail = listener.handle_callback(parsed.query)
                body = _result_page(ok, detail).encode("utf-8")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):   # quiet
                pass

        try:
            server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            # busy port: another listener (or the previous run) owns it
            self.log(f"[music] OAuth port {self.port} is busy ({e}); "
                     f"assuming a listener is already running")
            return False
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="spotify-oauth", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
