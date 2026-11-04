"""Shared test scaffolding: a fake Riko hub and fixtures.

Everything here talks only to 127.0.0.1 and needs no mic, GPU, or internet.
The fake hub speaks the subset of docs/DESIGN.md + docs/VOICE.md the clients
use, so these tests pass with or without the real hub/ package.
"""
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# ── Owner-state isolation (must run before any riko import) ──
# The desktop app persists runtime state next to the owner's config
# (~/.config/riko/listen_mode.json, roam_x.json, the client id + browser
# token, the avatar cache). Without this, tests read the OWNER's choices:
# an 'always' listening mode made every Session build a real AlwaysListener
# on the live mic (thousands of sounddevice callbacks, PortAudio teardown
# segfaults the whole pytest run). Point every override at one throwaway
# dir; per-test monkeypatches still win. RIKO_* uses setdefault so CI can
# pin its own values; the state dir itself is absolute, so HOME is safe.
_TEST_STATE_ROOT = tempfile.mkdtemp(prefix="riko-test-state-")
os.environ.setdefault("RIKO_APP_STATE_DIR", os.path.join(_TEST_STATE_ROOT, "appstate"))
os.environ.setdefault("RIKO_ROAM_STATE", os.path.join(_TEST_STATE_ROOT, "roam_x.json"))
os.environ.setdefault("RIKO_CONFIG_DIR", os.path.join(_TEST_STATE_ROOT, "config"))
os.environ.setdefault("RIKO_AVATAR_CACHE", os.path.join(_TEST_STATE_ROOT, "avatars"))
# Belt and braces: anything resolving XDG_CONFIG_HOME/riko also lands in
# the throwaway dir instead of the owner's ~/.config/riko.
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(_TEST_STATE_ROOT, "xdg"))

# ── No real audio devices ────────────────────────────────────
# Same reason: a real PortAudio stream in a test process races pytest's
# teardown and aborts it (exit 139/134 after 'passed'). Tests inject their
# fake (tests/test_listen_mode monkeypatches riko.asr.sd), so anything
# still reaching the sounddevice module is a bug — fail loudly, not loudly
# in a few seconds' worth of deprecation warnings.
try:
    import sounddevice as _sounddevice
except ImportError:  # headless CI without the wheel: nothing to guard
    pass
else:

    def _no_real_audio(*args, **kwargs):
        raise RuntimeError("tests must not open real audio devices: "
                           "inject a fake sd module (monkeypatch.setattr(asr, 'sd', fake))")

    for _fn in ("InputStream", "OutputStream", "play", "rec", "record"):
        setattr(_sounddevice, _fn, _no_real_audio)

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
    avatars, auth, hub-side voice (docs/VOICE.md), a state version counter and
    the client-tool channel + events long-poll (docs/API.md) — the contract
    the clients use."""

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
        self.emotion = None           # when set, chat replies carry this ChatResult.emotion
        self.actions = None           # when set, chat replies carry this ChatResult.actions
        self.speech = None            # when set, chat replies carry this ChatResult.speech
        self.register_count = 0       # how many register POSTs arrived
        self.last_auth = None         # Authorization header of the last API call
        # Client-tool channel (docs/API.md), server side, simplified:
        self.registered = {}          # client_id -> last register body
        self.heartbeats = []          # client_id of every heartbeat POST
        self.results = []             # JSON body of every /results POST
        self.unregistered = []        # client_ids that DELETEd themselves
        self._queued = {}             # client_id -> list of tool requests
        self._cond = threading.Condition()
        # Events ring (GET /api/events):
        self.events = []              # {seq, time, type, ...}
        self.event_seq = 0
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

            def _query(self):
                return urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                             if "?" in self.path else "")

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
                if path == "/api/events":
                    qp = self._query()
                    after = int(qp.get("after", ["0"])[0] or 0)
                    wait = min(float(qp.get("wait", ["0"])[0] or 0), 0.5)
                    deadline = time.monotonic() + wait
                    with outer._cond:
                        while not any(e["seq"] > after for e in outer.events) \
                                and time.monotonic() < deadline:
                            outer._cond.wait(0.05)
                        out = [e for e in outer.events if e["seq"] > after]
                    return self._send({"events": out,
                                       "last": max([e["seq"] for e in out], default=after)})
                if path == "/api/clients":
                    return self._send({"clients": [
                        {"client_id": cid, "tools": body.get("tools", [])}
                        for cid, body in outer.registered.items()],
                        "heartbeat_s": 30})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/requests", path):
                    cid = m[1]
                    wait = min(float(self._query().get("wait", ["25"])[0] or 25), 0.5)
                    with outer._cond:
                        if cid not in outer.registered:
                            return self._send({"detail": "register first"}, 404)
                        if not outer._queued.get(cid):
                            outer._cond.wait(wait)
                        out = outer._queued.pop(cid, [])
                    return self._send({"requests": out, "approval_timeout_s": 120})
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
                if m := re.fullmatch(r"/api/clients/([\w-]+)/register", self.path):
                    tools = body.get("tools")
                    if not isinstance(tools, list):
                        return self._send({"detail": "tools must be a list"}, 422)
                    for t in tools:
                        if not isinstance(t, dict) or not re.fullmatch(
                                r"[A-Za-z0-9_-]{1,64}", str(t.get("name") or "")):
                            return self._send({"detail": "bad tool name"}, 422)
                        if t.get("risk", "read") not in ("read", "act", "danger"):
                            return self._send({"detail": "bad tool risk"}, 422)
                    with outer._cond:
                        outer.registered[m[1]] = body
                        outer.register_count += 1
                        outer._cond.notify_all()
                    return self._send({"ok": True, "heartbeat_s": 30})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/heartbeat", self.path):
                    if m[1] not in outer.registered:
                        return self._send({"detail": "register first"}, 404)
                    outer.heartbeats.append(m[1])
                    return self._send({"ok": True, "heartbeat_s": 30})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/results", self.path):
                    if not body.get("request_id"):
                        return self._send({"detail": "request_id required"}, 422)
                    outer.results.append(body)
                    return self._send({"ok": True})
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
                if m := re.fullmatch(r"/api/clients/([\w-]+)", self.path.split("?")[0]):
                    with outer._cond:
                        outer.registered.pop(m[1], None)
                        outer.unregistered.append(m[1])
                        outer._cond.notify_all()
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
                resp = {"reply": target["greeting"] or f"{target['name']} here.",
                        "persona": target["id"], "switched": True,
                        "message_ids": [], "state_version": self.version,
                        "audio_url": greeting_audio}
                if self.emotion:  # tests can set .emotion to mimic an emotion-aware hub
                    resp["emotion"] = self.emotion
                if self.actions is not None:  # body-action-aware hub
                    resp["actions"] = self.actions
                if self.speech is not None:  # spoken-text-aware hub (hub/speech.py)
                    resp["speech"] = self.speech
                return resp
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
            resp = {"reply": CHAT_REPLY.format(name=self.personas[pid]["name"],
                                               text=body.get("text")),
                    "persona": pid, "switched": False,
                    "message_ids": ids, "state_version": self.version,
                    "audio_url": audio_url}
            if self.emotion:
                resp["emotion"] = self.emotion
            if self.actions is not None:  # body-action-aware hub
                resp["actions"] = self.actions
            if self.speech is not None:  # spoken-text-aware hub (hub/speech.py)
                resp["speech"] = self.speech
            return resp

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

    # ── client-tool channel test helpers ─────────────────────
    def push_request(self, client_id, tool, arguments=None, needs_approval=False,
                     summary=None, request_id=None):
        """Queue a tool call for a client, like the real dispatch() does."""
        req = {"request_id": request_id or f"req-{tool}-{len(self.results)}",
               "tool": tool, "arguments": arguments or {},
               "needs_approval": bool(needs_approval),
               "summary": summary or f"{tool} {json.dumps(arguments or {})}"}
        with self._cond:
            self._queued.setdefault(client_id, []).append(req)
            self._cond.notify_all()
        return req

    def push_event(self, etype, **fields):
        """Append one event to the GET /api/events ring."""
        with self._cond:
            self.event_seq += 1
            ev = {"seq": self.event_seq, "time": "2026-11-03T00:00:00Z",
                  "type": etype, **fields}
            self.events.append(ev)
            self._cond.notify_all()
        return ev

    def wait_for_register(self, client_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if client_id in self.registered:
                return True
            time.sleep(0.05)
        return False

    def wait_for_result(self, request_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in self.results:
                if r.get("request_id") == request_id:
                    return r
            time.sleep(0.05)
        return None

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        with self._cond:  # wake any half-open long-polls so shutdown is quick
            self._cond.notify_all()


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
