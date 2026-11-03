"""Shared test scaffolding: a fake Riko hub and fixtures.

Everything here talks only to 127.0.0.1 and needs no mic, GPU, or internet.
The fake hub speaks the subset of docs/DESIGN.md + docs/VOICE.md the clients
use, so these tests pass with or without the real hub/ package.
"""
import io
import json
import re
import socket
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HUB_KEY = "test-hub-key"
CHAT_REPLY = "[{name}] you said: {text}"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wav_bytes(seconds=0.2, rate=24000):
    """A tiny valid mono 16-bit WAV (what breeze/hub hand out, just small)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x20\x00\x00\x01" * int(rate * seconds))
    return buf.getvalue()


AUDIO_WAV = wav_bytes()


def persona(pid, name, keyword, greeting, tts_voice=""):
    return {"id": pid, "name": name, "keywords": [keyword], "system_prompt": f"You are {name}.",
            "greeting": greeting, "tts_voice": tts_voice, "avatar_version": 1,
            "voice": {"mode": "design", "description": f"{name}'s designed voice.",
                      "style": "", "ref_text": "", "has_reference": False,
                      "seed": 1234, "cfg_scale": None},
            "voice_version": 1,
            "updated_at": "2026-11-03T00:00:00Z"}


def avatar_bytes(pid, version):
    return b"glTF" + f"-avatar-{pid}-v{version}-".encode() + b"\0" * 32


class FakeHub:
    """Stand-in for hub/server.py: personas, keyword switching, history, import,
    avatars, auth, hub-side voice (docs/VOICE.md) and a state version counter —
    the contract the clients use."""

    SWITCH = re.compile(r"^(switch to|change to|become|persona|talk to|i want to talk to)\s+(.+)$")

    def __init__(self, api_key=HUB_KEY):
        self.api_key = api_key
        self.lock = threading.Lock()
        self.version = 1
        self.message_id = 0
        self.messages = []            # Message dicts, global order
        self.avatar_requests = []     # (id, version) of every avatar GET
        self.chat_requests = []       # JSON body of every /api/chat
        self.audio_requests = []      # path of every audio GET (or /api/tts body)
        self.last_auth = None         # Authorization header of the last API call
        # Voice knobs the tests flip:
        self.tts_enabled = True       # False -> every audio_url is null
        self.tts_reachable = True     # GET /api/tts/status reports this
        self.has_tts_status = True    # False -> an OLD hub: /api/tts/status is 404
        self.audio_mode = "ok"        # "ok" | "error" (503) | "missing" (404)
        self.personas = {
            "riko": persona("riko", "Riko", "riko", "Riko here. Miss me, senpai?"),
            "asuka": persona("asuka", "Asuka", "asuka", "Asuka here, senpai.", "af_sleepy"),
        }
        self.active = "riko"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # no keep-alive: stop() really ends the conversation

            def _send(self, payload, status=200, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json_body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _audio(self):
                """Reply for any audio GET / POST /api/tts, honouring audio_mode."""
                if outer.audio_mode == "error":
                    return self._send({"detail": "breeze is unreachable"}, 503)
                if outer.audio_mode == "missing":
                    return self._send({"detail": "no such audio"}, 404)
                return self._send(AUDIO_WAV, content_type="audio/wav")

            def _authed(self):
                outer.last_auth = self.headers.get("Authorization")
                if outer.api_key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {outer.api_key}"

            def do_GET(self):
                path = self.path.split("?")[0]
                if path != "/api/health" and not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                if path == "/api/health":
                    return self._send({"ok": True, "version": "1"})
                if path == "/api/state":
                    return self._send(outer.state())
                if path == "/api/personas":
                    return self._send(list(outer.personas.values()))
                if m := re.fullmatch(r"/api/personas/([\w-]+)", path):
                    p = outer.personas.get(m[1])
                    return self._send(p) if p else self._send({"detail": "no such persona"}, 404)
                if m := re.fullmatch(r"/api/personas/([\w-]+)/avatar", path):
                    p = outer.personas.get(m[1])
                    if not p:
                        return self._send({"detail": "no such persona"}, 404)
                    outer.avatar_requests.append((p["id"], p["avatar_version"]))
                    return self._send(avatar_bytes(p["id"], p["avatar_version"]),
                                      content_type="model/gltf-binary")
                if path == "/api/history":
                    return self._send({"persona": outer.active, "messages": list(outer.messages)})
                if path == "/api/tts/status":
                    if not outer.has_tts_status:
                        return self._send({"detail": "Not Found"}, 404)  # old hub, no voice
                    return self._send({"enabled": outer.tts_enabled,
                                       "reachable": outer.tts_reachable,
                                       "base_url": "http://fake-breeze:8137", "voices": 1})
                if m := re.fullmatch(r"/api/messages/(\d+)/audio", path):
                    outer.audio_requests.append(self.path)  # raw: keep any ?v=
                    msg = next((x for x in outer.messages if x["id"] == int(m[1])), None)
                    if not msg or msg["role"] != "assistant":
                        return self._send({"detail": "no such message"}, 404)
                    return self._audio()
                if m := re.fullmatch(r"/api/personas/([\w-]+)/greeting/audio", path):
                    outer.audio_requests.append(self.path)  # raw: keep any ?v=
                    p = outer.personas.get(m[1])
                    if not p:
                        return self._send({"detail": "no such persona"}, 404)
                    if (p.get("voice") or {}).get("mode") == "off":
                        return self._send({"detail": "persona voice is off"}, 409)
                    return self._audio()
                self._send({"detail": "not found"}, 404)

            def do_POST(self):
                if not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                body = self._json_body()
                if self.path == "/api/active":
                    p = outer.find(str(body.get("persona", "")))
                    if not p:
                        return self._send({"detail": "no such persona"}, 404)
                    with outer.lock:
                        outer.active = p["id"]
                        outer.version += 1
                    return self._send(outer.state())
                if self.path == "/api/chat":
                    return self._send(outer.chat(body))
                if self.path == "/api/tts":
                    outer.audio_requests.append(("tts", body.get("text", "")))
                    return self._audio()
                if self.path == "/api/history/import":
                    pid = body.get("persona") or outer.active
                    n = 0
                    with outer.lock:
                        for m in body.get("messages", []):
                            if m.get("role") not in ("user", "assistant"):
                                return self._send({"detail": "bad role"}, 422)
                            outer.message_id += 1
                            outer.messages.append({"id": outer.message_id, "persona": pid,
                                                   "role": m["role"], "content": m["content"],
                                                   "source": "import",
                                                   "created_at": "2026-11-03T00:00:00Z"})
                            n += 1
                        outer.version += 1
                    return self._send({"imported": n})
                self._send({"detail": "not found"}, 404)

            def do_PUT(self):
                if not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                m = re.fullmatch(r"/api/personas/([\w-]+)", self.path)
                p = outer.personas.get(m[1]) if m else None
                if not p:
                    return self._send({"detail": "no such persona"}, 404)
                body = self._json_body()
                with outer.lock:
                    for k in ("name", "system_prompt", "keywords", "greeting", "tts_voice"):
                        if k in body:
                            p[k] = body[k]
                    outer.version += 1
                return self._send(p)

            def do_DELETE(self):
                if not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                if self.path.split("?")[0] == "/api/history":
                    with outer.lock:
                        outer.messages = []
                        outer.version += 1
                    return self._send(b"", 204)
                self._send({"detail": "not found"}, 404)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    # ── helpers ──────────────────────────────────────────────
    def state(self):
        with self.lock:
            return {"version": self.version, "active": self.personas[self.active],
                    "personas": [{"id": p["id"], "name": p["name"]} for p in self.personas.values()],
                    "last_message_id": self.message_id, "llm_model": "test-model"}

    def find(self, needle):
        needle = (needle or "").strip().lower()
        for p in self.personas.values():
            if needle in (p["id"], p["name"].lower(), *[k.lower() for k in p["keywords"]]):
                return p
        return None

    def chat(self, body):
        text = str(body.get("text", "")).strip().lower().rstrip(".!?,")
        self.chat_requests.append(body)
        target = None
        with self.lock:
            if text in self.personas or self.find(text):
                target = self.find(text)
            else:
                m = self.SWITCH.match(text)
                if m:
                    target = self.find(m[2])
            if target:
                self.active = target["id"]
                self.version += 1
                greeting_audio = self._greeting_audio_url(target)
                return {"reply": target["greeting"] or f"{target['name']} here.",
                        "persona": target["id"], "switched": True,
                        "message_ids": [], "state_version": self.version,
                        "audio_url": greeting_audio}
            pid = self.active
            ids = []
            for role, content in (("user", body.get("text")), ("assistant",
                                 CHAT_REPLY.format(name=self.personas[pid]["name"],
                                                   text=body.get("text")))):
                self.message_id += 1
                ids.append(self.message_id)
                self.messages.append({"id": self.message_id, "persona": pid, "role": role,
                                      "content": content, "source": body.get("source", "test"),
                                      "created_at": "2026-11-03T00:00:00Z"})
            self.version += 1
            audio_url = None
            if self.tts_enabled:
                audio_url = f"/api/messages/{ids[-1]}/audio"
            return {"reply": CHAT_REPLY.format(name=self.personas[pid]["name"],
                                               text=body.get("text")),
                    "persona": pid, "switched": False,
                    "message_ids": ids, "state_version": self.version,
                    "audio_url": audio_url}

    def _greeting_audio_url(self, p):
        """ChatResult.audio_url for a switch (docs/VOICE.md), null with TTS
        turned off or the persona's voice set to off."""
        if not self.tts_enabled or (p.get("voice") or {}).get("mode") == "off":
            return None
        return f"/api/personas/{p['id']}/greeting/audio?v={p.get('voice_version', 1)}"

    def force_active(self, pid):
        """A switch made by some other client (web page, another PC)."""
        with self.lock:
            self.active = pid
            self.version += 1

    def bump_avatar(self, pid):
        with self.lock:
            self.personas[pid]["avatar_version"] += 1
            self.version += 1

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_cfg(hub_url):
    """A client config pointing at a fake hub; no mic or TTS involved."""
    return {
        "hub": {"mode": "remote", "url": hub_url, "api_key_env": "FAKE_HUB_KEY"},
        "llm": {"base_url": "http://127.0.0.1:1/v1", "api_key_env": "TEST_NO_SUCH_KEY",
                "model": "auto", "temperature": 0.8, "max_tokens": 1024, "max_history": 40},
        "asr": "remote",
        "remote_asr": {},
        "local_asr": {},
        "tts": "none",
        "openai_tts": {"url": "http://127.0.0.1:1/v1", "api_key_env": "TTS_API_KEY",
                       "model": "kokoro", "voice": "af_heart"},
        "sovits": {}, "fish": {},
        "desktop": {"width": 420, "height": 680, "bubble_seconds": 8},
    }


@pytest.fixture
def fake_hub():
    hub = FakeHub()
    yield hub
    hub.stop()


@pytest.fixture(autouse=True)
def clean_hub_client_globals():
    """hub_client keeps a module-level client + active persona; never leak them."""
    from riko import hub_client
    hub_client._reset()
    yield
    hub_client._reset()


@pytest.fixture
def hub_connected(fake_hub, monkeypatch, tmp_path):
    """A connected hub_client (the way check() leaves it), avatars cached in tmp."""
    from riko import hub_client
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    client = hub_client.connect(make_cfg(fake_hub.url))
    hub_client.note_active(client.state())
    return client
