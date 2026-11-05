"""Voice-round tests (docs/VOICE.md): per-persona Breeze-TTS-2 voices.

A fake breeze-server (stdlib, like the fake LLM in test_hub_brain.py) stands
on a free port: /health, POST /v1/audio/speech (returns a small WAV with the
same max-length header fields the real server streams, 404 unknown voice,
409 on demand), POST /v1/voices (multipart, records name + ref_text) and
GET /v1/voices. Everything else is the real hub.
"""
import io
import json
import socket
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import hub.server as server_mod
from hub.speech import SPEECH_RULES_VERSION
from hub.server import create_app
from test_hub_brain import FakeHubLLM

CLIP_SECONDS = 0.2  # every fake synthesis is this long (24 kHz mono 16-bit)


def broken_header_wav(seconds: float = CLIP_SECONDS, sample_rate: int = 24000) -> bytes:
    """A valid WAV whose RIFF/data length fields claim 0xFFFFFFFF — exactly
    what breeze-server streams and what browsers choke on (riko/tts.py had
    the same problem with Fish). The hub must re-encode before serving."""
    import numpy as np
    buf = io.BytesIO()
    n = int(sample_rate * seconds)
    sf.write(buf, np.sin(np.linspace(0.0, 40.0, n, dtype="float32")),
             sample_rate, format="WAV", subtype="PCM_16")
    raw = bytearray(buf.getvalue())
    raw[4:8] = b"\xff\xff\xff\xff"    # RIFF chunk size
    raw[40:44] = b"\xff\xff\xff\xff"  # data chunk size
    return bytes(raw)


def ref_wav(seconds: float = 4.0, sample_rate: int = 16000) -> bytes:
    """A proper reference clip for uploads (soundfile-readable, not broken)."""
    import numpy as np
    buf = io.BytesIO()
    n = int(sample_rate * seconds)
    sf.write(buf, np.sin(np.linspace(0.0, 60.0, n, dtype="float32")),
             sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class FakeBreeze:
    """Stand-in for breeze-server v0.1.6 (docs/VOICE.md 'Facts about breeze-server')."""

    def __init__(self):
        self.speech_requests = []   # JSON body of every /v1/audio/speech
        self.registrations = []     # {name, ref_text, bytes} of every POST /v1/voices
        self.health_calls = 0
        self.saved_voices = set()   # voice ids POST /v1/voices has registered
        self.busy_once = 0          # >0 -> answer 409 that many times (slots busy)
        self.fail_speech = False    # True -> 500 on speech
        self.fail_register = False  # True -> 500 on voice registration
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, payload, status=200, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    outer.health_calls += 1
                    return self._send({"status": "ok", "sample_rate": 24000})
                if self.path == "/v1/voices":
                    return self._send({"voices": sorted(outer.saved_voices)})
                self._send({"detail": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if self.path == "/v1/audio/speech":
                    body = json.loads(raw or b"{}")
                    outer.speech_requests.append(body)
                    voice = body.get("voice")
                    if voice and voice not in outer.saved_voices:
                        # unknown voice is a 404, never a silent fallback
                        return self._send({"detail": f"unknown voice: {voice}"}, 404)
                    if outer.busy_once > 0:
                        outer.busy_once -= 1
                        return self._send({"detail": "all slots busy"}, 409)
                    if outer.fail_speech:
                        return self._send({"detail": "boom"}, 500)
                    return self._send(broken_header_wav(), content_type="audio/wav")
                if self.path == "/v1/voices":
                    ctype = self.headers.get("Content-Type") or ""
                    msg = BytesParser(policy=policy.HTTP).parsebytes(
                        f"Content-Type: {ctype}\r\n\r\n".encode() + raw)
                    fields, audio_len = {}, 0
                    for part in msg.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if name is None:
                            continue
                        payload = part.get_payload(decode=True) or b""
                        if name == "ref_audio":
                            audio_len = len(payload)
                        else:
                            fields[name] = payload.decode(errors="replace")
                    if outer.fail_register:
                        return self._send({"detail": "register boom"}, 500)
                    voice_name = fields.get("name", "")
                    outer.registrations.append({"name": voice_name,
                                                "ref_text": fields.get("ref_text", ""),
                                                "bytes": audio_len})
                    outer.saved_voices.add(voice_name)
                    return self._send({"ok": True, "name": voice_name})
                self._send({"detail": "not found"}, 404)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def forget_voice(self, name: str):
        """Simulate breeze being reset: the saved voice is gone."""
        self.saved_voices.discard(name)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_llm():
    server = FakeHubLLM()
    yield server
    server.stop()


@pytest.fixture
def fake_breeze():
    server = FakeBreeze()
    yield server
    server.stop()


def make_client(tmp_path, llm=None, tts=None):
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"} if llm else None,
                     tts_defaults={"base_url": tts.base_url} if tts else None)
    return TestClient(app)


@pytest.fixture
def client(tmp_path, fake_llm, fake_breeze):
    return make_client(tmp_path, llm=fake_llm, tts=fake_breeze)


def add_persona(client, name="Asuka"):
    return client.post("/api/personas", json={
        "name": name, "system_prompt": f"You are {name}.", "greeting": f"{name} here!"})


def voice_of(client, pid="riko"):
    return client.get(f"/api/personas/{pid}").json()["voice"]


def upload_reference(client, pid, seconds=4.0, ref_text="Exactly what she says in the clip."):
    return client.put(f"/api/personas/{pid}/voice/reference",
                      files={"file": (ref := "clip.wav", ref_wav(seconds), "audio/wav")},
                      data={"ref_text": ref_text})


def readable_wav(content: bytes, expect_seconds: float | None = CLIP_SECONDS):
    """The hub must hand browsers a properly-headed WAV soundfile can read."""
    data, sample_rate = sf.read(io.BytesIO(content))
    duration = len(data) / sample_rate
    if expect_seconds is not None:
        assert abs(duration - expect_seconds) < 0.05, f"got {duration}s"
    assert data.ndim == 1  # mono
    return data, sample_rate


# ── chat + audio_url ─────────────────────────────────────────────────────────

def test_chat_returns_audio_url_and_never_waits_for_audio(client, fake_breeze):
    r = client.post("/api/chat", json={"text": "hi", "source": "web"})
    assert r.status_code == 200
    res = r.json()
    assert res["audio_url"] == f"/api/messages/{res['message_ids'][1]}/audio"
    # the chat itself must not have touched breeze — audio is made lazily
    assert fake_breeze.speech_requests == []


def test_message_audio_synthesizes_once_then_serves_cache(client, fake_breeze, tmp_path):
    client.post("/api/chat", json={"text": "hi"})
    r = client.get("/api/messages/2/audio")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    readable_wav(r.content)  # broken breeze header survived re-encoding
    r2 = client.get("/api/messages/2/audio")
    assert r2.status_code == 200
    assert r2.content == r.content
    assert len(fake_breeze.speech_requests) == 1  # made once, then cached
    assert (tmp_path / "hubdata" / "audio" / f"2-v1-r{SPEECH_RULES_VERSION}.wav").exists()  # -rN = speech rules version


def test_audio_cache_keyed_on_voice_version(client, fake_breeze, tmp_path):
    client.post("/api/chat", json={"text": "hi"})
    assert client.get("/api/messages/2/audio").status_code == 200
    assert len(fake_breeze.speech_requests) == 1
    v = client.put("/api/personas/riko/voice", json={"style": "slow and smug"})
    assert v.json()["voice_version"] == 2
    assert client.get("/api/messages/2/audio").status_code == 200
    assert len(fake_breeze.speech_requests) == 2  # re-synthesised for the new voice
    assert (tmp_path / "hubdata" / "audio" / f"2-v2-r{SPEECH_RULES_VERSION}.wav").exists()


def test_switch_gives_greeting_audio_url(client, fake_breeze, tmp_path):
    add_persona(client)
    r = client.post("/api/chat", json={"text": "asuka"})
    res = r.json()
    assert res["switched"] is True
    assert res["audio_url"] == "/api/personas/asuka/greeting/audio?v=1"
    g = client.get(res["audio_url"])
    assert g.status_code == 200
    readable_wav(g.content)
    assert fake_breeze.speech_requests[-1]["input"] == "Asuka here!"  # the greeting
    assert (tmp_path / "hubdata" / "audio" / f"greeting-asuka-v1-r{SPEECH_RULES_VERSION}.wav").exists()


def test_speak_false_skips_audio(client):
    assert client.post("/api/chat", json={"text": "hi", "speak": False}).json()["audio_url"] is None


def test_tts_off_gives_null_audio_and_503(tmp_path, fake_llm):
    client = make_client(tmp_path, llm=fake_llm, tts=None)  # no RIKO_TTS_BASE_URL
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["audio_url"] is None
    assert client.get("/api/messages/2/audio").status_code == 503
    assert client.post("/api/tts", json={"text": "hello"}).status_code == 503
    status = client.get("/api/tts/status").json()
    assert status["enabled"] is False and status["reachable"] is False


def test_persona_voice_off_chat_null_and_audio_409(client):
    client.post("/api/chat", json={"text": "hi"})
    assert client.put("/api/personas/riko/voice", json={"mode": "off"}).status_code == 200
    assert client.get("/api/messages/2/audio").status_code == 409
    assert client.post("/api/chat", json={"text": "again"}).json()["audio_url"] is None


def test_message_audio_404s(client):
    assert client.get("/api/messages/999/audio").status_code == 404
    client.post("/api/chat", json={"text": "hi"})
    assert client.get("/api/messages/1/audio").status_code == 404  # user message


# ── breeze request shapes ────────────────────────────────────────────────────

def test_design_request_carries_instructions_seed_and_cfg(client, fake_breeze):
    client.post("/api/chat", json={"text": "hi"})
    client.get("/api/messages/2/audio")
    req = fake_breeze.speech_requests[-1]
    voice = voice_of(client)
    assert "voice" not in req  # design = no voice id
    assert req["instructions"] == voice["description"]
    assert req["seed"] == voice["seed"]
    assert "cfg_scale" not in req  # null = breeze default
    client.put("/api/personas/riko/voice", json={"cfg_scale": 2.5})
    client.get("/api/messages/2/audio")  # voice_version 2 -> fresh synthesis
    assert fake_breeze.speech_requests[-1]["cfg_scale"] == 2.5


def test_clone_request_carries_voice_id(client, fake_breeze):
    add_persona(client)
    upload_reference(client, "asuka")
    r = client.post("/api/tts", json={"text": "hi there", "persona": "asuka"})
    assert r.status_code == 200
    req = fake_breeze.speech_requests[-1]
    assert req["voice"] == "riko-asuka-v2"  # riko-<id>-v<voice_version>
    assert req["input"] == "hi there"


def test_clone_404_reregisters_from_clip_and_retries(client, fake_breeze):
    add_persona(client)
    upload_reference(client, "asuka", ref_text="Reference transcript here.")
    fake_breeze.forget_voice("riko-asuka-v2")  # breeze was reset
    r = client.post("/api/tts", json={"text": "still works", "persona": "asuka"})
    assert r.status_code == 200
    assert len(fake_breeze.speech_requests) == 2  # 404 then retry
    re_reg = fake_breeze.registrations[-1]
    assert re_reg["name"] == "riko-asuka-v2"
    assert re_reg["ref_text"] == "Reference transcript here."  # from the stored clip


def test_busy_409_is_retried(client, fake_breeze):
    fake_breeze.busy_once = 1  # all slots busy once, then free
    r = client.post("/api/tts", json={"text": "retry me"})
    assert r.status_code == 200
    assert len(fake_breeze.speech_requests) == 2
    fake_breeze.busy_once = 99  # permanently busy -> 503 after the retries
    busy = client.post("/api/tts", json={"text": "never gets a slot"})
    assert busy.status_code == 503


def test_breeze_failure_maps_to_502(client, fake_breeze):
    fake_breeze.fail_speech = True
    r = client.post("/api/tts", json={"text": "boom"})
    assert r.status_code == 502
    assert "detail" in r.json()


def test_unreachable_breeze_is_503(tmp_path, fake_llm):
    client = make_client(tmp_path, llm=fake_llm, tts=None)
    client.put("/api/settings", json={"tts": {"base_url": f"http://127.0.0.1:{free_port()}"}})
    r = client.post("/api/tts", json={"text": "hello?"})
    assert r.status_code == 503
    assert client.get("/api/tts/status").json()["reachable"] is False


# ── reference upload / lock / preview ────────────────────────────────────────

def test_reference_upload_validation_leaves_persona_unchanged(client, fake_breeze, tmp_path):
    add_persona(client)
    before = client.get("/api/personas/asuka").json()
    bad = [
        ({"files": {"file": ("c.wav", ref_wav(1.0), "audio/wav")}, "data": {"ref_text": "x"}},
         "too short"),
        ({"files": {"file": ("c.wav", ref_wav(31.0), "audio/wav")}, "data": {"ref_text": "x"}},
         "too long"),
        ({"files": {"file": ("c.wav", b"this is not audio", "audio/wav")}, "data": {"ref_text": "x"}},
         "bad file"),
        ({"files": {"file": ("c.wav", ref_wav(4.0), "audio/wav")}, "data": {}},
         "missing ref_text"),
        ({"files": {"file": ("c.wav", ref_wav(4.0), "audio/wav")}, "data": {"ref_text": "   "}},
         "blank ref_text"),
    ]
    for kwargs, why in bad:
        r = client.put("/api/personas/asuka/voice/reference", **kwargs)
        assert r.status_code == 400, why
    assert client.get("/api/personas/asuka").json() == before  # nothing moved
    assert fake_breeze.registrations == []  # nothing was registered
    assert not (tmp_path / "hubdata" / "voices" / "asuka.wav").exists()


def test_reference_breeze_down_changes_nothing(client, fake_breeze, tmp_path):
    add_persona(client)
    fake_breeze.fail_register = True
    r = upload_reference(client, "asuka")
    assert r.status_code == 502
    after = client.get("/api/personas/asuka").json()
    assert after["voice"]["mode"] == "design" and after["voice_version"] == 1
    assert not (tmp_path / "hubdata" / "voices" / "asuka.wav").exists()


def test_reference_upload_get_delete_flow(client, fake_breeze, tmp_path):
    add_persona(client)
    v0 = client.get("/api/state").json()["version"]
    r = upload_reference(client, "asuka", ref_text="My exact words.")
    assert r.status_code == 200
    p = r.json()
    assert p["voice"]["mode"] == "clone"
    assert p["voice"]["ref_text"] == "My exact words."
    assert p["voice"]["has_reference"] is True
    assert p["voice_version"] == 2
    assert client.get("/api/state").json()["version"] == v0 + 1
    reg = fake_breeze.registrations[-1]
    assert reg["name"] == "riko-asuka-v2" and reg["ref_text"] == "My exact words."
    assert reg["bytes"] > 1000  # the clip actually went over as multipart
    assert (tmp_path / "hubdata" / "voices" / "asuka.wav").exists()

    got = client.get("/api/personas/asuka/voice/reference")
    assert got.status_code == 200
    readable_wav(got.content, expect_seconds=4.0)

    d = client.delete("/api/personas/asuka/voice/reference")
    assert d.status_code == 200
    assert d.json()["voice"]["mode"] == "design"
    assert d.json()["voice"]["has_reference"] is False
    assert d.json()["voice_version"] == 3
    assert client.get("/api/personas/asuka/voice/reference").status_code == 404
    assert client.delete("/api/personas/asuka/voice/reference").status_code == 404


def test_lock_freezes_designed_voice(client, fake_breeze, tmp_path):
    v0 = client.get("/api/state").json()["version"]
    r = client.post("/api/personas/riko/voice/lock", json={"text": "Locked voice, senpai."})
    assert r.status_code == 200
    p = r.json()
    assert p["voice"]["mode"] == "clone"
    assert p["voice"]["ref_text"] == "Locked voice, senpai."  # text == ref_text
    assert p["voice_version"] == 2
    assert client.get("/api/state").json()["version"] == v0 + 1
    # generated with the design voice first, then registered as the new clone
    assert "voice" not in fake_breeze.speech_requests[-1]
    assert fake_breeze.speech_requests[-1]["input"] == "Locked voice, senpai."
    assert fake_breeze.registrations[-1]["name"] == "riko-riko-v2"
    assert (tmp_path / "hubdata" / "voices" / "riko.wav").exists()
    # already a clone -> nothing left to lock
    assert client.post("/api/personas/riko/voice/lock", json={}).status_code == 409
    # and the clone voice now speaks with the registered id
    client.post("/api/chat", json={"text": "hi"})
    client.get("/api/messages/2/audio")
    assert fake_breeze.speech_requests[-1]["voice"] == "riko-riko-v2"


def test_preview_uses_greeting_by_default_and_is_not_cached(client, fake_breeze, tmp_path):
    r = client.post("/api/personas/riko/voice/preview", json={})
    assert r.status_code == 200
    readable_wav(r.content)
    assert fake_breeze.speech_requests[-1]["input"] == "Riko here. Miss me, senpai?"
    r2 = client.post("/api/personas/riko/voice/preview", json={"text": "custom line"})
    assert r2.status_code == 200
    assert fake_breeze.speech_requests[-1]["input"] == "custom line"
    assert list((tmp_path / "hubdata" / "audio").glob("*.wav")) == []  # never cached


def test_preview_design_override_reaches_breeze_and_stores_nothing(client, fake_breeze, tmp_path):
    """The web page previews the panel's UNSAVED values: the `voice` override
    must reach the breeze request and change nothing on the persona."""
    v0 = client.get("/api/state").json()["version"]
    stored = voice_of(client)
    r = client.post("/api/personas/riko/voice/preview", json={
        "text": "audition line",
        "voice": {"mode": "design", "description": "A gruff old captain.",
                  "style": "slow and grumpy", "seed": 4242, "cfg_scale": 3.5},
    })
    assert r.status_code == 200
    readable_wav(r.content)
    req = fake_breeze.speech_requests[-1]
    assert req["input"] == "audition line"
    assert "voice" not in req  # still design: no clone id
    assert req["instructions"] == "A gruff old captain. slow and grumpy"
    assert req["seed"] == 4242
    assert req["cfg_scale"] == 3.5
    # nothing stored: persona voice and both versions untouched
    after = voice_of(client)
    assert after == stored
    assert client.get("/api/personas/riko").json()["voice_version"] == 1
    assert client.get("/api/state").json()["version"] == v0
    assert list((tmp_path / "hubdata" / "audio").glob("*.wav")) == []  # not cached


def test_preview_override_validation(client, fake_breeze):
    v0 = client.get("/api/state").json()["version"]
    bad = [
        {"voice": "design"},                # not an object
        {"voice": {"mode": "robot"}},       # bad mode
        {"voice": {"seed": -1}},            # negative seed
        {"voice": {"seed": "big"}},         # non-int seed
        {"voice": {"seed": True}},          # bool is not a seed
        {"voice": {"cfg_scale": 0}},        # out of range
        {"voice": {"cfg_scale": "loud"}},   # not a number
        {"voice": {"wat": 1}},              # unknown field
    ]
    for body in bad:
        r = client.post("/api/personas/riko/voice/preview", json=body)
        assert r.status_code == 422, body
        assert "detail" in r.json()
    assert fake_breeze.speech_requests == []  # rejects synthesise nothing
    assert client.get("/api/state").json()["version"] == v0  # and store nothing


def test_preview_clone_override_without_reference_is_422(client, fake_breeze):
    add_persona(client)
    r = client.post("/api/personas/asuka/voice/preview",
                    json={"voice": {"mode": "clone", "style": "whispery"}})
    assert r.status_code == 422
    assert "reference" in r.json()["detail"]
    assert fake_breeze.speech_requests == []


def test_preview_clone_override_with_reference(client, fake_breeze):
    add_persona(client)
    upload_reference(client, "asuka")  # mode clone, voice_version 2
    r = client.post("/api/personas/asuka/voice/preview",
                    json={"voice": {"mode": "clone", "style": "whispery"}})
    assert r.status_code == 200
    req = fake_breeze.speech_requests[-1]
    assert req["voice"] == "riko-asuka-v2"  # stored voice_version, not a new one
    assert req["instructions"] == "whispery"  # style override is the delivery direction
    after = voice_of(client, "asuka")  # the override is not stored
    assert after["mode"] == "clone" and after["style"] != "whispery"
    assert client.get("/api/personas/asuka").json()["voice_version"] == 2


def test_preview_override_can_audition_off_voice_in_design(client, fake_breeze):
    """Saved mode off must not block Preview, and the override switches mode
    just for this one synthesis."""
    assert client.put("/api/personas/riko/voice", json={"mode": "off"}).status_code == 200
    r = client.post("/api/personas/riko/voice/preview",
                    json={"voice": {"mode": "design", "description": "Bright and shy."}})
    assert r.status_code == 200
    assert fake_breeze.speech_requests[-1]["instructions"] == "Bright and shy."
    assert voice_of(client)["mode"] == "off"  # still off — override thrown away


# ── POST /api/tts ────────────────────────────────────────────────────────────

def test_tts_route_length_limit_and_validation(client):
    # raw-text cap is 5000 (the SPOKEN text is capped shorter by
    # speech.MAX_SPOKEN_CHARS); a wall of one word survives cleaning
    assert client.post("/api/tts", json={"text": "x" * 5000}).status_code == 200
    too_long = client.post("/api/tts", json={"text": "x" * 5001})
    assert too_long.status_code == 422
    assert client.post("/api/tts", json={"text": "  "}).status_code == 422
    assert client.post("/api/tts", json={"text": "hi", "persona": "ghost"}).status_code == 404


# ── cache cap + cleanup ──────────────────────────────────────────────────────

def test_cache_evicts_oldest_over_cap(client, fake_breeze, tmp_path, monkeypatch):
    # each fake clip is ~9.6 KB; cap lets ~2.5 of them live
    monkeypatch.setattr(server_mod, "MAX_AUDIO_CACHE_BYTES", 25_000)
    audio_dir = tmp_path / "hubdata" / "audio"
    for i in range(3):
        client.post("/api/chat", json={"text": f"turn {i}"})
        mid = client.get("/api/history").json()["messages"][-1]["id"]
        assert client.get(f"/api/messages/{mid}/audio").status_code == 200
    remaining = sorted(f.name for f in audio_dir.glob("*.wav"))
    assert len(remaining) == 2  # oldest evicted, newest kept
    assert f"6-v1-r{SPEECH_RULES_VERSION}.wav" in remaining


def test_delete_persona_removes_its_audio_and_clip(client, fake_breeze, tmp_path):
    add_persona(client)
    upload_reference(client, "asuka")
    client.post("/api/chat", json={"text": "hi", "persona": "asuka"})
    assert client.get("/api/messages/2/audio").status_code == 200
    assert client.get("/api/personas/asuka/greeting/audio").status_code == 200
    audio_dir = tmp_path / "hubdata" / "audio"
    assert len(list(audio_dir.glob("*.wav"))) == 2
    assert client.delete("/api/personas/asuka").status_code == 204
    assert list(audio_dir.glob("*.wav")) == []
    assert not (tmp_path / "hubdata" / "voices" / "asuka.wav").exists()


def test_clear_history_removes_message_audio_keeps_greeting(client, fake_breeze, tmp_path):
    add_persona(client)
    client.post("/api/chat", json={"text": "hi", "persona": "asuka"})
    assert client.get("/api/messages/2/audio").status_code == 200
    assert client.get("/api/personas/asuka/greeting/audio").status_code == 200
    assert client.delete("/api/history?persona=asuka").status_code == 204
    audio_dir = tmp_path / "hubdata" / "audio"
    names = [f.name for f in audio_dir.glob("*.wav")]
    assert names == [f"greeting-asuka-v1-r{SPEECH_RULES_VERSION}.wav"]  # message audio went with the rows


# ── voice editing rules ──────────────────────────────────────────────────────

def test_voice_put_validation_and_bumps(client):
    v0 = client.get("/api/state").json()["version"]
    r = client.put("/api/personas/ghost/voice", json={"style": "x"})
    assert r.status_code == 404
    assert client.put("/api/personas/riko/voice", json={"mode": "robot"}).status_code == 422
    assert client.put("/api/personas/riko/voice", json={"mode": "clone"}).status_code == 422  # no clip
    assert client.put("/api/personas/riko/voice", json={"seed": "big"}).status_code == 422
    assert client.put("/api/personas/riko/voice", json={"cfg_scale": "loud"}).status_code == 422
    assert client.put("/api/personas/riko/voice", json={"wat": 1}).status_code == 422
    assert client.get("/api/state").json()["version"] == v0  # rejects changed nothing

    r = client.put("/api/personas/riko/voice", json={"description": "A smug cat.", "seed": 77})
    assert r.status_code == 200
    assert r.json()["voice"]["description"] == "A smug cat."
    assert r.json()["voice"]["seed"] == 77
    assert r.json()["voice_version"] == 2
    assert client.get("/api/state").json()["version"] == v0 + 1
    # resending the same values is not a change: no version bump
    same = client.put("/api/personas/riko/voice", json={"description": "A smug cat.", "seed": 77})
    assert same.json()["voice_version"] == 2
    assert client.get("/api/state").json()["version"] == v0 + 1
    # has_reference is read-only even if the client insists
    sly = client.put("/api/personas/riko/voice", json={"has_reference": True})
    assert sly.status_code == 422


def test_seed_persona_voice_seeded(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    voice = voice_of(client)
    assert voice["mode"] == "design"
    assert voice["description"] == server_mod.SEED_VOICE_DESCRIPTION
    assert isinstance(voice["seed"], int) and voice["seed"] > 0
    assert voice["cfg_scale"] is None and voice["has_reference"] is False


# ── settings tts block ───────────────────────────────────────────────────────

def test_settings_tts_blank_means_default(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    s = client.get("/api/settings").json()
    assert s["tts"]["base_url"] == fake_breeze.base_url and s["tts"]["enabled"] is True
    assert s["defaults"]["tts"]["base_url"] == fake_breeze.base_url
    assert s["tts_source"]["base_url"] == "default"

    saved = client.put("/api/settings", json={"tts": {"base_url": "http://elsewhere:8137",
                                                      "timeout": 5}})
    assert saved.json()["tts"]["base_url"] == "http://elsewhere:8137"
    assert saved.json()["tts"]["timeout"] == 5
    assert saved.json()["tts_source"]["base_url"] == "saved"

    blanked = client.put("/api/settings", json={"tts": {"base_url": ""}})
    assert blanked.json()["tts"]["base_url"] == fake_breeze.base_url  # env default again
    assert blanked.json()["tts_source"]["base_url"] == "default"

    assert client.put("/api/settings", json={"tts": {"enabled": False}}).status_code == 200
    assert client.post("/api/chat", json={"text": "hi"}).json()["audio_url"] is None
    assert client.post("/api/tts", json={"text": "hi"}).status_code == 503
    client.put("/api/settings", json={"tts": {"enabled": True}})
    assert client.post("/api/tts", json={"text": "hi"}).status_code == 200


def test_settings_tts_validation_and_reset(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    assert client.put("/api/settings", json={"tts": "on"}).status_code == 422
    assert client.put("/api/settings", json={"tts": {"enabled": "yes"}}).status_code == 422
    assert client.put("/api/settings", json={"tts": {"timeout": "fast"}}).status_code == 422
    assert client.put("/api/settings", json={"tts": {"timeout": 0}}).status_code == 422

    client.put("/api/settings", json={"tts": {"base_url": "http://elsewhere:8137"}})
    out = client.post("/api/settings/reset", json={"tts": True}).json()
    assert out["tts"]["base_url"] == fake_breeze.base_url  # env default again
    assert all(v == "default" for v in out["tts_source"].values())
    assert client.post("/api/settings/reset", json={}).status_code == 422


def test_settings_test_reports_tts(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    out = client.post("/api/settings/test").json()
    assert out["ok"] is True and out["tts"] == {"ok": True}
    assert fake_breeze.health_calls > 0
    client.put("/api/settings", json={"tts": {"enabled": False}})
    assert client.post("/api/settings/test").json()["tts"] == \
        {"ok": False, "error": "TTS disabled in settings"}


def test_tts_status(client, fake_breeze):
    add_persona(client)
    upload_reference(client, "asuka")
    s = client.get("/api/tts/status").json()
    assert s == {"enabled": True, "reachable": True,
                 "base_url": fake_breeze.base_url, "voices": 1}


# ── migration of an old (v1) database ───────────────────────────────────────

def test_old_db_gets_voice_defaults(tmp_path):
    import sqlite3
    from hub.store import GENERIC_VOICE_DESCRIPTION, _SCHEMA_V1

    db = tmp_path / "hubdata" / "hub.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA_V1)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO personas(id,name,keywords,system_prompt,greeting,tts_voice,"
                 "avatar_version,has_avatar,created_at,updated_at) "
                 "VALUES('nova','Nova','[\"nova\"]','You are Nova.','Nova here.','','0','0',"
                 "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO meta(key,value) VALUES('active','nova')")
    conn.execute("INSERT INTO meta(key,value) VALUES('version','9')")
    conn.commit()
    conn.close()

    client = make_client(tmp_path)  # opens the v1 db -> migrates to v2
    p = client.get("/api/personas/nova").json()
    assert p["voice"]["mode"] == "design"
    assert p["voice"]["description"] == GENERIC_VOICE_DESCRIPTION
    assert isinstance(p["voice"]["seed"], int) and p["voice"]["seed"] > 0
    assert p["voice_version"] == 1
    assert client.get("/api/state").json()["version"] == 9  # migration keeps the state version


# ── long text: one request, Breeze splits it itself ──────────────────────────

def test_long_utterance_goes_to_breeze_in_one_request(fake_breeze):
    """Breeze-TTS-2.cpp v0.1.6 has NO input length cap: the server splits
    long text on sentence boundaries itself (split_chars, default 600) and
    conditions each piece on the first so the voice holds (docs/server.md;
    see the hub/voice.py header). The client must therefore send the whole
    utterance in ONE request — chunking client-side would lose that voice
    conditioning and pad silence between the pieces."""
    from hub.voice import Breeze
    breezy = Breeze({"base_url": fake_breeze.base_url})
    text = "This sentence is long enough to matter, senpai. " * 55  # ~2600 chars
    wav = breezy.speech(text, instructions="Bright and warm.")
    assert len(fake_breeze.speech_requests) == 1
    assert fake_breeze.speech_requests[-1]["input"] == text
    # the reply is a proper WAV with real header lengths (breeze streams
    # max-length placeholders; reencode_wav fixes them before anyone caches
    # or plays the clip)
    data, rate = sf.read(io.BytesIO(wav))
    assert rate == 24000 and abs(len(data) / rate - CLIP_SECONDS) < 0.05
