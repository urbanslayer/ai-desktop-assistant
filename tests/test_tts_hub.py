"""tts: hub — the hub makes the audio (docs/VOICE.md), we fetch and play it.

Same fake hub as the other client tests, now with the voice routes:
audio_url on ChatResults, GET message/greeting audio, POST /api/tts and
GET /api/tts/status (which an OLD hub answers 404).
"""
import time

import soundfile as sf

from conftest import CHAT_REPLY, free_port, make_cfg
from riko import hub_client, session, tts, viewer


def hub_turn_env(monkeypatch, fake_hub, tmp_path):
    """turn_env from test_session, but with tts: hub: VOICE_FILE points into
    tmp and viewer.play is recorded instead of touching an audio device."""
    monkeypatch.setenv("RIKO_PORT", str(free_port()))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    cfg = make_cfg(fake_hub.url)
    cfg["tts"] = "hub"
    monkeypatch.setattr(tts, "VOICE_FILE", tmp_path / "riko.wav")
    played = []
    monkeypatch.setattr(viewer, "play", lambda wav, text=None: played.append((wav, text)))
    hub_client.connect(cfg)
    return cfg, fake_hub, played


def wait_played(played, timeout=10):
    deadline = time.monotonic() + timeout
    while not played:
        assert time.monotonic() < deadline, "nothing was ever played"
        time.sleep(0.05)
    return played.pop(0)


def test_hub_mode_plays_the_downloaded_audio(monkeypatch, fake_hub, tmp_path):
    cfg, fake_hub, played = hub_turn_env(monkeypatch, fake_hub, tmp_path)
    s = session.Session(cfg, source="desktop")
    s.submit_text("say something")

    wav, text = wait_played(played)
    assert text == CHAT_REPLY.format(name="Riko", text="say something")
    assert str(wav).endswith("riko.wav") and wav.exists()
    data, rate = sf.read(wav)
    assert len(data) / rate > 0  # a real WAV made it through soundfile
    # the audio came from the hub route for that assistant message
    assert fake_hub.audio_requests == ["/api/messages/2/audio"]


def test_null_audio_url_is_bubble_only(monkeypatch, fake_hub, tmp_path):
    cfg, fake_hub, played = hub_turn_env(monkeypatch, fake_hub, tmp_path)
    fake_hub.tts_enabled = False  # hub TTS off -> audio_url is null
    s = session.Session(cfg, source="desktop")
    s.submit_text("quiet today")

    wav, text = wait_played(played)
    assert wav is None and text == CHAT_REPLY.format(name="Riko", text="quiet today")
    assert not (tmp_path / "riko.wav").exists()
    assert fake_hub.audio_requests == []


def test_hub_audio_error_is_bubble_only_and_printed_once(monkeypatch, fake_hub, tmp_path,
                                                         capsys):
    cfg, fake_hub, played = hub_turn_env(monkeypatch, fake_hub, tmp_path)
    fake_hub.audio_mode = "error"  # breeze unreachable -> hub says 503
    s = session.Session(cfg, source="desktop")
    s.submit_text("first")
    wav1, text1 = wait_played(played)
    s.submit_text("second")
    wav2, text2 = wait_played(played)

    assert wav1 is None and wav2 is None  # bubbles both times, no crash
    out = capsys.readouterr().out
    assert out.count("hub audio failed") == 1  # printed once, not per turn

    # and the worker is fully alive: audio recovers when the hub does
    fake_hub.audio_mode = "ok"
    s.submit_text("third")
    wav3, _ = wait_played(played)
    assert wav3 is not None
    out = capsys.readouterr().out
    assert "hub audio failed" not in out  # silence again after the recovery


def test_switch_greeting_uses_the_greeting_audio(monkeypatch, fake_hub, tmp_path):
    cfg, fake_hub, played = hub_turn_env(monkeypatch, fake_hub, tmp_path)
    s = session.Session(cfg, source="desktop")

    s.switch_persona("asuka")  # what riko-ctl / the tray do
    wav, text = wait_played(played)
    assert text == "Asuka here, senpai." and wav is not None
    assert "/api/personas/asuka/greeting/audio?v=1" in fake_hub.audio_requests

    # a keyword switch goes through the chat result's audio_url instead
    played.clear()
    fake_hub.audio_requests.clear()
    s.submit_text("switch to riko")
    wav, text = wait_played(played)
    assert text == "Riko here. Miss me, senpai?" and wav is not None
    assert "/api/personas/riko/greeting/audio?v=1" in fake_hub.audio_requests


def test_persona_voice_off_is_bubble_only(monkeypatch, fake_hub, tmp_path):
    cfg, fake_hub, played = hub_turn_env(monkeypatch, fake_hub, tmp_path)
    fake_hub.personas["asuka"]["voice"]["mode"] = "off"
    s = session.Session(cfg, source="desktop")

    s.switch_persona("asuka")
    wav, text = wait_played(played)
    assert wav is None and text == "Asuka here, senpai."


def test_old_hub_audio_404_is_bubble_only(monkeypatch, fake_hub, tmp_path):
    """A hub whose breeze lost this reply's audio (404): bubble, no crash."""
    cfg, fake_hub, played = hub_turn_env(monkeypatch, fake_hub, tmp_path)
    fake_hub.audio_mode = "missing"
    s = session.Session(cfg, source="desktop")
    s.submit_text("hello?")
    wav, text = wait_played(played)
    assert wav is None and text == CHAT_REPLY.format(name="Riko", text="hello?")


def test_check_hub_status_paths(monkeypatch, fake_hub, capsys):
    cfg = make_cfg(fake_hub.url)
    cfg["tts"] = "hub"
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    hub_client.connect(cfg)

    tts.check(cfg)  # enabled + reachable: happy line, never exits
    assert "her voice is ready" in capsys.readouterr().out

    fake_hub.tts_enabled = False
    tts.check(cfg)
    assert "turned off" in capsys.readouterr().out

    fake_hub.tts_enabled = True
    fake_hub.tts_reachable = False
    tts.check(cfg)
    assert "cannot reach breeze" in capsys.readouterr().out

    fake_hub.has_tts_status = False  # an old hub without the route
    tts.check(cfg)
    out = capsys.readouterr().out
    assert "too old for voice" in out and "Update the hub" in out

    fake_hub.has_tts_status = True
    fake_hub.tts_reachable = True


def test_hub_self_test_route_reaches_the_hub(monkeypatch, fake_hub):
    """`python -m riko.tts`' hub path: POST /api/tts straight to the hub."""
    cfg = make_cfg(fake_hub.url)
    cfg["tts"] = "hub"
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    client = hub_client.connect(cfg)
    wav_bytes = client.tts_audio("one test line")
    assert wav_bytes[:4] == b"RIFF"
    assert fake_hub.audio_requests == [("tts", "one test line")]
