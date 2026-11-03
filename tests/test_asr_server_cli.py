"""Fast unit tests for asr_server's CLI plumbing — no real model is loaded."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import asr_server  # noqa: E402


class FakeWhisperModel:
    calls = []

    def __init__(self, model_size_or_path, device=None, compute_type=None,
                 download_root=None, **kwargs):
        FakeWhisperModel.calls.append(dict(model_size_or_path=model_size_or_path,
                                           device=device, compute_type=compute_type,
                                           download_root=download_root))
        self.model = type("M", (), {"device": "fake-device"})()


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
