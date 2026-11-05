"""Fast unit tests for asr_server's CLI plumbing — no real model is loaded."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import asr_server  # noqa: E402


class FakeWhisperModel:
    calls = []
    kwargs = {}

    def __init__(self, model_size_or_path, device=None, compute_type=None,
                 download_root=None, **kwargs):
        FakeWhisperModel.calls.append(dict(model_size_or_path=model_size_or_path,
                                           device=device, compute_type=compute_type,
                                           download_root=download_root))
        self.model = type("M", (), {"device": "fake-device"})()

    def transcribe(self, audio, *, language=None, initial_prompt=None,
                   temperature=0.0, beam_size=5, vad_filter=True):
        # deliberately no `hotwords` parameter: build_app must notice
        FakeWhisperModel.kwargs = dict(language=language, initial_prompt=initial_prompt,
                                       temperature=temperature)
        seg = type("S", (), {"text": "farina", "no_speech_prob": 0.1,
                             "avg_logprob": -0.2, "compression_ratio": 1.2})()
        info = type("I", (), {"duration": 1.0, "language": "en"})()
        return iter([seg]), info


@pytest.fixture(autouse=True)
def no_ambient_asr_key(monkeypatch):
    """build_app reads ASR_API_KEY from the environment at build time; a real
    key in the owner's .env or shell would silently enable auth and turn every
    keyless request below into a 401. Tests that want auth set the key
    explicitly (see tests/test_asr_server.py, which runs a real subprocess)."""
    monkeypatch.delenv("ASR_API_KEY", raising=False)


@pytest.fixture
def fake_whisper(monkeypatch):
    import faster_whisper
    FakeWhisperModel.calls = []
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    return FakeWhisperModel


def test_model_dir_flag_reaches_download_root(fake_whisper, monkeypatch):
    monkeypatch.delenv("ASR_MODEL_DIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["asr_server.py", "--model", "tiny.en",
                                      "--device", "cpu", "--model-dir", "/data/models"])
    args = asr_server.parse_args()
    asr_server.build_app(args.model, args.device, args.compute_type, model_dir=args.model_dir)
    assert fake_whisper.calls == [dict(model_size_or_path="tiny.en", device="cpu",
                                       compute_type="int8", download_root="/data/models")]


def test_model_dir_env_var(fake_whisper, monkeypatch):
    monkeypatch.setenv("ASR_MODEL_DIR", "/mnt/models")
    monkeypatch.setattr(sys, "argv", ["asr_server.py", "--device", "cpu"])
    args = asr_server.parse_args()
    assert args.model_dir == "/mnt/models"
    asr_server.build_app(args.model, args.device, args.compute_type, model_dir=args.model_dir)
    assert fake_whisper.calls[0]["download_root"] == "/mnt/models"


def test_no_model_dir_means_default_cache(fake_whisper, monkeypatch):
    monkeypatch.delenv("ASR_MODEL_DIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["asr_server.py", "--device", "cpu"])
    args = asr_server.parse_args()
    assert args.model_dir is None
    asr_server.build_app(args.model, args.device, args.compute_type, model_dir=args.model_dir)
    assert fake_whisper.calls[0]["download_root"] is None


def _post_transcribe(client, **data):
    return client.post("/v1/audio/transcriptions",
                       files={"file": ("a.wav", b"fake audio", "audio/wav")},
                       data=data)


def test_prompt_reaches_initial_prompt_and_hotwords_are_ignored(fake_whisper):
    """This fake's transcribe has NO `hotwords` parameter (older
    faster-whisper): build_app detects it, the field is accepted, and the
    call still works with prompt -> initial_prompt."""
    from fastapi.testclient import TestClient

    app = asr_server.build_app("base.en", "cpu", "int8")
    with TestClient(app) as client:
        r = _post_transcribe(client, prompt="Riko, Furina, senpai.", hotwords="Furina")
        assert r.status_code == 200 and r.json()["text"] == "farina"
        assert fake_whisper.kwargs["initial_prompt"] == "Riko, Furina, senpai."


def test_hotwords_forwarded_when_faster_whisper_supports_them(fake_whisper, monkeypatch):
    """faster-whisper >= 1.1: `prompt` (initial_prompt) and `hotwords` compose."""
    import faster_whisper
    from fastapi.testclient import TestClient

    class ModernWhisper(fake_whisper):
        def transcribe(self, audio, *, language=None, initial_prompt=None,
                       hotwords=None, temperature=0.0, beam_size=5, vad_filter=True):
            fake_whisper.kwargs = dict(initial_prompt=initial_prompt, hotwords=hotwords)
            seg = type("S", (), {"text": "Furina", "no_speech_prob": 0.1,
                                 "avg_logprob": -0.2, "compression_ratio": 1.2})()
            info = type("I", (), {"duration": 1.0, "language": "en"})()
            return iter([seg]), info

    monkeypatch.setattr(faster_whisper, "WhisperModel", ModernWhisper)
    app = asr_server.build_app("base.en", "cpu", "int8")
    with TestClient(app) as client:
        r = _post_transcribe(client, prompt="Riko, Furina.", hotwords="Furina")
        assert r.status_code == 200
        assert fake_whisper.kwargs == {"initial_prompt": "Riko, Furina.",
                                       "hotwords": "Furina"}
        r = _post_transcribe(client)  # neither given: nothing forwarded at all
        assert fake_whisper.kwargs == {"initial_prompt": None, "hotwords": None}
