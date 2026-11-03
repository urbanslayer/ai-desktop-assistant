"""Her ears: record your voice (push-to-talk) and turn it into text.

asr: remote -> Faster-Whisper on another machine (server/asr_server.py, or any
               OpenAI-compatible /v1/audio/transcriptions server). The mic is
               always the one on this PC; only the WAV clip goes over the network.
asr: local  -> Faster-Whisper on this PC
asr: groq   -> Groq's online Whisper
"""
import io
import os
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import OpenAI

from riko.config import need_key

SAMPLE_RATE = 16000                # what Whisper expects
GROQ_MODEL = "whisper-large-v3-turbo"
MIN_SECONDS = 0.3                  # shorter than this is a mis-press, not speech

_whisper = None  # Faster-Whisper model for asr: local, loaded once by check()
_remote = None   # OpenAI client pointed at the remote ASR server


def check(cfg, fatal=True):
    """Get the ears ready. With fatal=False a dead remote server only prints a warning."""
    global _whisper, _remote
    mode = cfg["asr"]
    if mode == "groq":
        need_key("GROQ_API_KEY", "asr: groq")
    elif mode == "remote":
        r = cfg["remote_asr"]
        _remote = OpenAI(base_url=r["url"], api_key=os.getenv(r.get("api_key_env") or "", "") or "not-needed",
                         timeout=120)
        try:
            # asr_server.py and speaches both answer /v1/models; that's enough to know it's up.
            _remote.models.list()
            print(f"[setup] remote ASR is up at {r['url']}")
        except Exception as e:
            msg = (f"[setup] remote ASR not reachable at {r['url']} ({type(e).__name__}). "
                   "Start server/asr_server.py on that machine, or set asr: local.")
            if fatal:
                raise SystemExit(msg)
            print(msg + " Voice input will fail until it is up; typing still works.")
    else:
        from faster_whisper import WhisperModel  # imported here so remote/groq users never load it
        loc = cfg.get("local_asr", {})
        model, device = loc.get("model", "base.en"), loc.get("device", "cpu")
        print(f"[asr] loading Faster-Whisper {model} on {device} (first run downloads it)...")
        _whisper = WhisperModel(model, device=device, compute_type="int8")


class Recorder:
    """Mic recording you can start and stop from anywhere (keyboard, hotkey, GUI)."""

    def __init__(self):
        self._chunks = []
        self._stream = None
        self._lock = threading.Lock()

    @property
    def recording(self):
        return self._stream is not None

    def start(self):
        with self._lock:
            if self._stream is not None:
                return
            self._chunks = []
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                          callback=lambda data, *_: self._chunks.append(data.copy()))
            self._stream.start()

    def stop(self):
        """Stop and return the clip as WAV bytes, or None if it was too short to be speech."""
        with self._lock:
            if self._stream is None:
                return None
            self._stream.stop()
            self._stream.close()
            self._stream = None
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return None
        audio = np.concatenate(chunks)
        seconds = len(audio) / SAMPLE_RATE
        print(f"[asr] recorded {seconds:.1f}s")
        if seconds < MIN_SECONDS:
            return None
        wav = io.BytesIO()
        sf.write(wav, audio, SAMPLE_RATE, format="WAV")
        return wav.getvalue()


def record():
    """Terminal push-to-talk: Enter to start, Enter to stop. Returns WAV bytes, or None."""
    input("\nPress ENTER to talk...")
    rec = Recorder()
    rec.start()
    input("Recording... press ENTER to stop")
    return rec.stop()


def transcribe(wav_bytes, cfg):
    mode = cfg["asr"]
    if mode == "groq":
        groq = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
        text = groq.audio.transcriptions.create(model=GROQ_MODEL, file=("you.wav", wav_bytes)).text
    elif mode == "remote":
        r = cfg["remote_asr"]
        extra = {"language": r["language"]} if r.get("language") else {}
        text = _remote.audio.transcriptions.create(model=r.get("model", "whisper-1"),
                                                   file=("you.wav", wav_bytes), **extra).text
    else:
        segments, _ = _whisper.transcribe(io.BytesIO(wav_bytes))
        text = "".join(s.text for s in segments)  # each segment already starts with a space
    return text.strip()


if __name__ == "__main__":
    # Talk into your mic and see what she hears: uv run python -m riko.asr
    from riko.config import load_config
    cfg = load_config()
    check(cfg)
    clip = record()
    print("You said:", transcribe(clip, cfg) if clip else "(nothing)")
