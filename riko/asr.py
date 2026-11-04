"""Her ears: record your voice (push-to-talk) and turn it into text.

asr: remote -> Faster-Whisper on another machine (server/asr_server.py, or any
               OpenAI-compatible /v1/audio/transcriptions server). The mic is
               always the one on this PC; only the WAV clip goes over the network.
asr: local  -> Faster-Whisper on this PC
asr: groq   -> Groq's online Whisper
"""
import io
import os
import re
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import OpenAI

from riko import vad as vad_mod
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
            chunks = []  # the PortAudio callback thread appends to this exact list
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                    callback=lambda data, *_: chunks.append(data.copy()))
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self._chunks = chunks
            self._stream = stream

    def stop(self):
        """Stop and return the clip as WAV bytes, or None if it was too short to be speech."""
        with self._lock:
            if self._stream is None:
                return None
            stream, self._stream = self._stream, None
            chunks, self._chunks = self._chunks, []
            try:
                stream.stop()  # after close() returns the callback thread is done, so `chunks` is final
                stream.close()
            except Exception as e:  # a device hiccup must not leave recording stuck on
                print(f"[asr] error closing the mic stream: {e}")
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


class AlwaysListener:
    """Hands-free ears: one continuous 16 kHz mono stream, cut into utterances
    by the VAD in riko/vad.py. Each finished segment goes to on_segment(wav)
    — the SAME path push-to-talk uses (submit_audio -> transcribe -> turn),
    tagged source='vad' so the turn can tell VAD noise from a deliberate clip.

    Deliberately silent to the UI: the point of always mode is that there is
    no listening indicator, so this never emits a 'listening' status.
    pause()/resume() drop frames and reset the segmenter (she is speaking, or
    a turn is thinking); suspend()/release() hand the mic device itself to
    the push-to-talk Recorder while Space is held. A device error mid-stream
    retries with backoff; start() raises so the caller can fall back to
    push-to-talk. Thread-safe: everything runs on one reader thread.
    """

    def __init__(self, on_segment, cfg=None, sd_module=None, vad=None,
                 retry_delay=0.5, retry_max=10.0):
        self.on_segment = on_segment
        self._sd = sd_module or sd
        self._cfg = dict(vad_mod.DEFAULTS, **(cfg or {}))
        self._vad = vad          # None -> backend chosen at start()
        self.vad_name = getattr(vad, "name", "injected") if vad else None
        self._segmenter = None
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self._pause = threading.Event()      # frames dropped, stream stays open
        self._suspend = threading.Event()    # device handed to the Recorder
        self._resume = threading.Event()     # released / resumed, loop wakes up
        self._retry_delay = retry_delay
        self._retry_max = retry_max
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._thread is not None

    def start(self):
        """Open the stream and start the reader thread; raises if the mic won't open."""
        with self._lock:
            if self._thread is not None:
                return
            if self._vad is None:
                self._vad, self.vad_name = vad_mod.make_vad()
                print(f"[listen] VAD backend: {self.vad_name}")
            self._segmenter = vad_mod.Segmenter(
                threshold=self._cfg["threshold"],
                min_speech_ms=self._cfg["min_speech_ms"],
                silence_ms=self._cfg["silence_ms"],
                pre_roll_ms=self._cfg["pre_roll_ms"],
                max_seconds=self._cfg["max_seconds"],
                min_voiced_ms=self._cfg["min_voiced_ms"],
            )
            self._stop.clear()
            self._pause.clear()
            self._suspend.clear()
            self._resume.set()
            self._stream = self._open_stream()  # first failure must reach the caller
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="riko-vad")
            self._thread.start()

    def _open_stream(self):
        stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                      dtype="float32")
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream

    def close(self):
        """Stop the reader and release the device (shutdown)."""
        with self._lock:
            thread, self._thread = self._thread, None
            stream, self._stream = self._stream, None
        self._stop.set()
        self._resume.set()  # wake a suspended reader so join is quick
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=3)

    def pause(self):
        """Drop frames and forget the half-built segment (she is speaking)."""
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def suspend(self):
        """Give the mic device to the push-to-talk Recorder (Space held)."""
        self._suspend.set()

    def release(self):
        if self._suspend.is_set():
            self._suspend.clear()
            self._resume.set()

    # ── the reader thread ─────────────────────────────────────
    def _run(self):
        delay = self._retry_delay
        while not self._stop.is_set():
            if self._suspend.is_set():
                self._close_quiet()
                self._resume.wait(timeout=0.2)
                continue
            try:
                if self._stream is None:
                    self._stream = self._open_stream()
                    delay = self._retry_delay
                    print("[listen] mic stream back up")
                frame, _overflowed = self._stream.read(vad_mod.FRAME_SAMPLES)
            except Exception as e:
                if self._stop.is_set() or self._suspend.is_set():
                    continue
                self._close_quiet()
                print(f"[listen] mic stream failed ({type(e).__name__}: {e}); "
                      f"retrying in {delay:.1f}s")
                if self._stop.wait(delay) or self._suspend.wait(0) :
                    continue
                delay = min(delay * 2, self._retry_max)
                continue
            if self._pause.is_set() or self._suspend.is_set():
                self._segmenter.reset()
                continue
            end = self._segmenter.push(np.asarray(frame).reshape(-1),
                                       float(self._vad(frame)))
            if end is None:
                continue
            if end.audio is None:
                print(f"[asr] always-listen: dropped a {end.seconds:.1f}s blip "
                      f"({end.voiced_seconds:.2f}s of speech)")
                continue
            wav = io.BytesIO()
            sf.write(wav, end.audio, SAMPLE_RATE, format="WAV")
            print(f"[asr] always-listen: caught {end.seconds:.1f}s")
            try:
                self.on_segment(wav.getvalue())
            except Exception as e:  # a segment must not kill the reader
                print(f"[asr] always-listen: segment dropped ({type(e).__name__}: {e})")

    def _close_quiet(self):
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


# Whisper's favourites when it hears nothing: classic word-hallucinations on
# short noise segments. Compared case-insensitively, without surrounding
# punctuation; the subtitles-by pattern catches the credit-line family.
NOISE_TEXTS = {
    "thank you", "thanks", "thank you!", "thanks for watching", "thanks for watching!",
    "thank you for watching", "you", "bye", "bye bye", "bye.", "goodbye",
    "yes", "so", "okay", "ok", "hmm", "oh", "the end", "please subscribe",
    "you. ", "mm hm", "mhm", "aha", "ooh", "wow",
}
_SUBS_LINE = re.compile(r"subt?itles? (?:by|readings? by)|captioned? (?:by|in)", re.I)
NOISE_HALLUCINATION_MAX_SECONDS = 1.5  # only short segments are "noise"


def hallucination_reason(text, wav_bytes, noise_phrases=True):
    """Why this transcript is probably Whisper inventing words: a reason str,
    or None when the text looks real. Empty/punctuation-only always count;
    the classic word-hallucinations only on short (< ~1.5 s) segments.

    noise_phrases=False skips the classic-phrase/subtitles blocklist: a
    deliberate push-to-talk "Thank you." is a real sentence, not a hallucination.
    Only VAD segments (TV noise) should ever drop those."""
    t = (text or "").strip()
    if not t:
        return "no words"
    if not any(c.isalnum() for c in t):
        return "punctuation only"
    if not noise_phrases:
        return None
    seconds = 0.0
    try:
        seconds = sf.info(io.BytesIO(wav_bytes)).duration
    except Exception:
        pass
    if seconds < NOISE_HALLUCINATION_MAX_SECONDS:
        core = t.lower().strip(" \t.!?~*-–—…")
        if core in NOISE_TEXTS or _SUBS_LINE.search(t):
            return f"noise phrase on a {seconds:.1f}s clip"
    return None


def transcribe(wav_bytes, cfg):
    mode = cfg["asr"]
    if mode == "groq":
        groq = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
        text = groq.audio.transcriptions.create(model=GROQ_MODEL, file=("you.wav", wav_bytes)).text
    elif mode == "remote":
        if _remote is None:
            raise RuntimeError("remote ASR was never set up: call asr.check(cfg) first "
                               "(it failed? start server/asr_server.py on that machine, or set asr: local)")
        r = cfg["remote_asr"]
        extra = {"language": r["language"]} if r.get("language") else {}
        text = _remote.audio.transcriptions.create(model=r.get("model", "whisper-1"),
                                                   file=("you.wav", wav_bytes), **extra).text
    else:
        if _whisper is None:
            raise RuntimeError("local ASR was never loaded: call asr.check(cfg) first")
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
