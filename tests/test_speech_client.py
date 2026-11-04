"""Spoken-text tests (client half): the riko/speech.py mirror, tts wiring.

Same case table as the hub (tests/data/speech_cases.json): the local
openai/sovits/fish backends must never read URLs or markdown either, so
riko/tts.py speaks ChatResult.speech when the hub sent one and applies the
identical cleaner when it did not.
"""
import io
import json
import math
import time
from pathlib import Path

import pytest
import soundfile as sf

import hub.speech as hub_speech
import riko.speech as client_speech
from conftest import free_port, make_cfg
from riko import hub_client, session, tts, viewer

CASES = json.loads((Path(__file__).parent / "data" / "speech_cases.json").read_text())


def tiny_wav(seconds=0.2, rate=24000) -> bytes:
    buf = io.BytesIO()
    n = int(rate * seconds)
    sf.write(buf, [math.sin(i / 9.0) for i in range(n)], rate,
             format="WAV", subtype="PCM_16")
    return buf.getvalue()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_speech_case_table_mirror(case):
    assert client_speech.speech_text(case["input"]) == case["expect"]


def test_mirrors_stay_identical():
    # one module per side (hub deploys standalone); the rules version is part
    # of the hub's audio cache names, so a mismatch means the client's
    # local-TTS text would drift from the hub's clips.
    assert client_speech.SPEECH_RULES_VERSION == hub_speech.SPEECH_RULES_VERSION
    for case in CASES:
        assert client_speech.speech_text(case["input"]) == hub_speech.speech_text(case["input"])


@pytest.fixture
def openai_capture(monkeypatch, tmp_path):
    """tts: openai with the backend call captured (no TTS server running)."""
    calls = []
    monkeypatch.setattr(tts, "VOICE_FILE", tmp_path / "riko.wav")
    monkeypatch.setattr(tts, "_openai",
                        lambda text, o, voice=None: calls.append(text) or tiny_wav())
    cfg = make_cfg("http://127.0.0.1:1")
    cfg["tts"] = "openai"
    return cfg, calls


def test_speak_prefers_hub_speech(openai_capture):
    cfg, calls = openai_capture
    wav = tts.speak("Found one!\n\nhttps://x.example/a.gif", cfg,
                    speech="Found one, it's on your screen!")
    assert wav and wav.exists()
    assert calls == ["Found one, it's on your screen!"]


def test_speak_cleans_when_hub_gave_no_speech(openai_capture):
    cfg, calls = openai_capture
    assert tts.speak("Found one! https://x.example/a.gif [1]", cfg)
    assert calls == ["Found one!"]


def test_speak_never_reads_a_bare_url(openai_capture):
    cfg, calls = openai_capture
    assert tts.speak("https://x.example/a.gif", cfg)
    assert calls == [client_speech.LINK_FALLBACK]
    assert "http" not in calls[0]


def test_speak_silent_for_wordless_text(openai_capture):
    cfg, calls = openai_capture
    assert tts.speak("\U0001F602\U0001F602", cfg) is None  # emoji only -> no audio
    assert tts.speak("", cfg) is None
    assert calls == []


# ── the session hands the worker's TTS what the hub said to say ──────────────

def test_session_passes_chat_speech_to_local_tts(monkeypatch, fake_hub, tmp_path):
    """An embodied turn on tts: openai: tts.speak gets answer + ChatResult.speech."""
    monkeypatch.setenv("RIKO_PORT", str(free_port()))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    cfg = make_cfg(fake_hub.url)
    cfg["tts"] = "openai"
    fake_hub.speech = "Found one, it's on your screen!"
    spoken = []
    monkeypatch.setattr(tts, "speak",
                        lambda text, cfg, voice=None, speech=None:
                        spoken.append((text, speech)) or None)
    played = []
    monkeypatch.setattr(viewer, "play",
                        lambda wav, text=None, emotion="neutral", actions=None:
                        played.append(text))
    hub_client.connect(cfg)
    s = session.Session(cfg, source="desktop")
    s.submit_text("find me a cat gif")
    deadline = time.monotonic() + 20
    while not spoken and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spoken, "the worker never reached the TTS"
    text, speech = spoken[0]
    assert speech == "Found one, it's on your screen!"  # exactly what the hub said
    assert "http" not in speech
    # and the bubble still shows the FULL reply, not the shortened speech
    from conftest import CHAT_REPLY
    assert played and played[0] == CHAT_REPLY.format(name="Riko",
                                                     text="find me a cat gif")
