"""Her body: a tiny web server for the avatar page (client/index.html).

The page asks /next four times a second. The answer carries a voice clip (if
one is waiting) plus UI events: what state she is in, what she said, what
she heard. When a clip ends the page calls /done, so the next turn only
starts after she has finished talking.

The desktop app and riko-ctl talk to the same server:
    POST /say            {"text": "..."}   you typed something
    POST /control/<cmd>  {}                talk / talk-start / talk-stop / type / forget / quit ...

No page open? The clip plays on your speakers instead.
"""
import json
import os
import queue
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import sounddevice as sd
import soundfile as sf

from riko.config import ROOT

HOST = "127.0.0.1"  # 127.0.0.1, not localhost: behaves the same on Windows and Linux
PORT = int(os.environ.get("RIKO_PORT") or 8080)  # override with RIKO_PORT=... (see start())

_clips = queue.Queue()          # voice clips waiting for the page
_events = queue.Queue()         # UI events waiting for the page
_finished = threading.Event()   # set when the page says a clip ended
_page_last_seen = float("-inf")  # when the page last asked /next (time.monotonic); never yet
_avatar = None                  # the .vrm file named in character_config.yaml
_settings = {}                  # the desktop: section of the config, for the page
_handlers = {"say": None, "control": None}  # set by on_say() / on_control()


def on_say(fn):
    """Register fn(text) for text typed into the page."""
    _handlers["say"] = fn


def on_control(fn):
    """Register fn(cmd) for /control/<cmd> requests. Return value is sent back as JSON."""
    _handlers["control"] = fn


def emit(kind, **data):
    """Send a UI event to the page (status changes, speech bubbles, errors)."""
    _events.put({"type": kind, **data})


def page_open():
    return time.monotonic() - _page_last_seen < 2


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        global _page_last_seen
        if self.path == "/avatar.vrm":
            if _avatar is None or not _avatar.exists():
                return self._send(b'{"error": "avatar not loaded"}', "application/json", 503)
            return self._send(_avatar.read_bytes(), "application/octet-stream")
        if self.path == "/settings":
            return self._send(json.dumps(_settings).encode(), "application/json")
        if self.path != "/next":
            return super().do_GET()  # the page and the voice clip, from client/
        _page_last_seen = time.monotonic()
        try:
            clip = _clips.get_nowait()
        except queue.Empty:
            clip = None
        events = []
        while True:
            try:
                events.append(_events.get_nowait())
            except queue.Empty:
                break
        self._send(json.dumps({"audio": clip, "events": events}).encode(), "application/json")

    def do_POST(self):
        if self.path == "/done":
            _finished.set()
            return self._send(b"{}", "application/json")
        # Everything else changes state, so insist on JSON: a random web page can't send that
        # cross-origin without a CORS preflight, which this server never approves.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(b'{"error": "send application/json"}', "application/json", 415)
        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return self._send(b'{"error": "bad json"}', "application/json", 400)
        if not isinstance(body, dict):
            return self._send(b'{"error": "expected a JSON object"}', "application/json", 400)

        try:
            if self.path == "/say":
                if not _handlers["say"]:
                    return self._send(b'{"error": "nothing is listening"}', "application/json", 503)
                text = str(body.get("text", "")).strip()
                if text:
                    _handlers["say"](text)
                return self._send(b'{"ok": true}', "application/json")
            if self.path.startswith("/control/"):
                if not _handlers["control"]:
                    return self._send(b'{"error": "nothing is listening"}', "application/json", 503)
                result = _handlers["control"](self.path.removeprefix("/control/"))
                return self._send(json.dumps(result if result is not None else {"ok": True}).encode(),
                                  "application/json")
        except Exception as e:  # a handler bug should not hang up on the caller
            print(f"[viewer] {self.path} raised {type(e).__name__}: {e}")
            return self._send(json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(),
                              "application/json", 500)
        self._send(b'{"error": "not found"}', "application/json", 404)

    def _read_body(self):
        """Read exactly Content-Length bytes, or until a chunked body ends."""
        length = self.headers.get("Content-Length")
        if length is not None:
            try:
                remaining = max(0, int(length))
            except ValueError:
                return b""
            data = b""
            while remaining:  # read() can return short
                chunk = self.rfile.read(remaining)
                if not chunk:
                    break
                data += chunk
                remaining -= len(chunk)
            return data
        if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
            return b""
        data = b""
        while True:
            try:
                size = int(self.rfile.readline().split(b";")[0], 16)
            except ValueError:
                break
            if size <= 0:
                self.rfile.readline()  # trailing CRLF
                break
            data += self.rfile.read(size)
            self.rfile.readline()  # CRLF after each chunk
        return data

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the terminal readable


def start(cfg):
    global _avatar, _settings, PORT
    _avatar = ROOT / cfg["vrm_model"]
    _settings = dict(cfg.get("desktop") or {})
    PORT = int(os.environ.get("RIKO_PORT") or PORT)  # re-read: tests (and scripts) may set it late
    if not _avatar.exists():
        sys.exit(f"[setup] Avatar not found: {_avatar}. Check vrm_model in character_config.yaml.")
    try:
        server = ThreadingHTTPServer((HOST, PORT), partial(_Handler, directory=str(ROOT / "client")))
    except OSError:
        sys.exit(f"[setup] Port {PORT} is busy (is Riko already running?). Close it, set RIKO_PORT to "
                 f"another port, or change PORT in riko/viewer.py.")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[viewer] avatar: {cfg['vrm_model']}")
    print(f"[viewer] avatar page: http://{HOST}:{PORT}  (open it, then click once)")
    return server


def play(wav_path, text=None):
    """Show her line and play her voice on the avatar page (or the speakers if no page is open).

    wav_path may be None (tts: none): then only the speech bubble shows. Returns when she is done.
    """
    if wav_path is None:
        if text:
            emit("say", text=text)
        return
    duration = sf.info(wav_path).duration
    if page_open():
        if text:
            emit("say", text=text, seconds=duration)
        _finished.clear()
        _clips.put(f"audio/{wav_path.name}?v={time.time()}")  # ?v= so the browser never replays the old clip
        if not _finished.wait(timeout=duration + 10):
            print("[viewer] the page never said the clip ended; carrying on")
    else:
        print("[viewer] no avatar page open, playing on speakers")
        data, sample_rate = sf.read(wav_path)
        sd.play(data, sample_rate)
        sd.wait()
