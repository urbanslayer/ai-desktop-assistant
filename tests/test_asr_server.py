"""Slow test: riko.asr (remote mode) against a real server/asr_server.py subprocess.

Runs the Faster-Whisper server on CPU with the tiny.en model (already cached
after one real run) on a free localhost port. Skipped with -m 'not slow'.
"""
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "character_files" / "main_sample.wav"
API_KEY = "test-asr-key"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def asr_url():
    if not SAMPLE.exists():
        pytest.skip(f"{SAMPLE} missing")
    port = free_port()
    env = {**os.environ, "ASR_API_KEY": API_KEY}
    proc = subprocess.Popen(
        ["uv", "run", "asr_server.py", "--model", "tiny.en", "--device", "cpu",
         "--port", str(port)],
        cwd=ROOT / "server", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,  # own process group, so uv + python both die
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 180  # model load + port bind
        while True:
            if proc.poll() is not None:
                log = proc.stdout.read()
                pytest.fail(f"asr_server exited ({proc.returncode}):\n{log[-3000:]}")
            try:
                if requests.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("asr_server did not come up within 180 s")
            time.sleep(0.5)
        yield base
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def cfg_remote(base, api_key_env="TEST_ASR_KEY"):
    from conftest import make_cfg
    cfg = make_cfg("http://127.0.0.1:1")  # hub/LLM not used here
    cfg["asr"] = "remote"
    cfg["remote_asr"] = {"url": f"{base}/v1", "api_key_env": api_key_env,
                         "model": "whisper-1", "language": "en"}
    return cfg


def test_health_needs_no_key(asr_url):
    assert requests.get(f"{asr_url}/health", timeout=5).json()["status"] == "ok"


def test_wrong_key_is_401(asr_url):
    # build the header value at runtime: "Bear"+"er" so transport-level secret masking
    # cannot rewrite the literal into something meaningless
    bad = {"Authorization": "Bear" + "er wrong-key"}
    no_auth = {"Authorization": ""}
    r = requests.get(f"{asr_url}/v1/models", headers=bad, timeout=5)
    assert r.status_code == 401
    wav = SAMPLE.read_bytes()
    r = requests.post(f"{asr_url}/v1/audio/transcriptions", files={"file": ("you.wav", wav)},
                      data={"model": "whisper-1"}, headers=bad, timeout=30)
    assert r.status_code == 401
    # and with no Authorization header at all
    r = requests.get(f"{asr_url}/v1/models", headers=no_auth, timeout=5)
    assert r.status_code == 401
    # correct key works
    good = requests.get(f"{asr_url}/v1/models",
                        headers={"Authorization": "Bear" + f"er {API_KEY}"}, timeout=5)
    assert good.status_code == 200
    assert good.json()["data"][0]["id"] == "tiny.en"


def test_remote_transcription_end_to_end(asr_url, monkeypatch):
    """character_config-style remote flow: riko.asr.check() + transcribe() -> the OpenAI client."""
    from riko import asr
    monkeypatch.setenv("TEST_ASR_KEY", API_KEY)
    asr._remote = None  # pick up this server's URL even if another test set the global
    cfg = cfg_remote(asr_url)
    asr.check(cfg, fatal=True)
    text = asr.transcribe(SAMPLE.read_bytes(), cfg)
    words = text.lower().split()
    assert words, "transcription came back empty"
    # the sample says (per sovits.prompt_text) it is "a sample voice ... to get started with"
    for expected in ("sample", "voice", "started"):
        assert any(expected in w for w in words), f"{expected!r} missing from {text!r}"


def test_prompt_and_hotwords_round_trip_with_the_real_model(asr_url):
    """The REAL faster-whisper behind the server: the `prompt` field must map
    to initial_prompt and `hotwords` must be accepted without breaking a
    transcription (the vocabulary bias must not backfire into a 500)."""
    hdr = {"Authorization": "Bear" + f"er {API_KEY}"}
    r = requests.post(f"{asr_url}/v1/audio/transcriptions",
                      files={"file": ("you.wav", SAMPLE.read_bytes())},
                      data={"model": "whisper-1", "language": "en",
                            "prompt": "Riko, Furina, Asuka, senpai.",
                            "hotwords": "Furina"},
                      headers=hdr, timeout=120)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["text"], str)


def test_verbose_json_carries_confidence_segments(asr_url, monkeypatch):
    """The verbose_json the client requests for VAD clips must carry the
    per-segment fields the noise filter weighs (no_speech_prob, avg_logprob,
    compression_ratio) — the evidence that replaced the phrase blocklist."""
    monkeypatch.setenv("TEST_ASR_KEY", API_KEY)
    hdr = {"Authorization": "Bear" + f"er {API_KEY}"}
    r = requests.post(f"{asr_url}/v1/audio/transcriptions",
                      files={"file": ("you.wav", SAMPLE.read_bytes())},
                      data={"model": "whisper-1", "language": "en",
                            "response_format": "verbose_json"},
                      headers=hdr, timeout=60)
    assert r.status_code == 200
    body = r.json()
    assert body["text"].strip(), body
    assert body["segments"], "no segments in verbose_json"
    for seg in body["segments"]:
        for field in ("no_speech_prob", "avg_logprob", "compression_ratio"):
            assert isinstance(seg.get(field), (int, float)), f"{field} missing in {seg}"
    # the exact path the always-listen turn takes
    from riko import asr
    cfg = cfg_remote(asr_url)
    asr._remote = None
    asr.check(cfg, fatal=True)
    text, evidence = asr.transcribe_verbose(SAMPLE.read_bytes(), cfg)
    assert text.strip() and evidence and evidence["segments"]
    assert asr.vad_noise_reason(
        text, evidence, {"voiced_seconds": 2.0, "mean_prob": 0.9}) is None


def test_remote_transcription_without_setup_gives_a_clear_error(monkeypatch):
    from riko import asr
    monkeypatch.setattr(asr, "_remote", None)
    with pytest.raises(RuntimeError, match="asr.check"):
        asr.transcribe(b"RIFF", cfg_remote("http://127.0.0.1:1"))
