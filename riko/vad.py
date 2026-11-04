"""Voice activity detection for always-listen mode (no mic, no threads here).

Everything in this module is pure: you feed it 512-sample frames at 16 kHz
and it tells you when an utterance starts and ends. The mic lives in
riko/asr.py (AlwaysListener); keeping the decision logic here means the
segmentation is testable with synthetic audio arrays.

Backends (both return a speech probability in [0, 1] per frame):

  SileroVad  — the Silero VAD ONNX that faster-whisper ships in its own
               package (no downloads at runtime), run through onnxruntime
               with the recurrent state carried across frames, exactly how
               the model wants to be streamed. Preferred when installed.
  EnergyVad  — adaptive energy gate: a slow noise-floor estimate plus
               open/close hysteresis. The fallback when onnxruntime or
               faster-whisper is missing; needs no models at all.

Segmenter turns the per-frame probabilities into utterances:
  * starts  once speech prob > threshold for >= min_speech_ms,
  * includes pre_roll_ms of audio from BEFORE that decision,
  * ends    after silence_ms of consecutive non-speech frames,
  * caps    an utterance at max_seconds,
  * drops   segments whose voiced audio is shorter than min_voiced_ms.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000         # what both Whisper and Silero VAD want
FRAME_SAMPLES = 512         # Silero's native frame (32 ms at 16 kHz)
FRAME_MS = 1000.0 * FRAME_SAMPLES / SAMPLE_RATE

DEFAULTS = {
    "threshold": 0.5,       # speech prob above this counts as a voiced frame
    "min_speech_ms": 200,   # keep talking this long before we call it speech
    "silence_ms": 800,      # this much quiet ends the utterance
    "pre_roll_ms": 300,     # audio kept from before the speech started
    "max_seconds": 30.0,    # hard cap per utterance
    "min_voiced_ms": 500,   # less real speech than this: drop the segment
}


def frame_count(ms: float) -> int:
    """Frames covering ms (rounded up; 0 ms = no requirement)."""
    return max(0, int(-(-float(ms) // FRAME_MS)))


class SileroVad:
    """Streaming Silero VAD on the ONNX file faster-whisper already ships.

    The model is recurrent (h/c states + 64 samples of context from the
    previous frame), so we keep that state between frames — feeding it one
    frame at a time with a reset state is what its batch mode does NOT do.
    """

    CONTEXT_SAMPLES = 64

    def __init__(self, model_path: str | None = None, session=None):
        import onnxruntime as ort

        if session is not None:
            self.session = session
        else:
            if model_path is None:
                from faster_whisper.utils import get_assets_path
                import os

                model_path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.log_severity_level = 4
            self.session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"], sess_options=opts)
        # Zero states shaped from the model itself (v5: [2,1,128], v6: [1,1,128]).
        self._states = {}
        for inp in self.session.get_inputs():
            if inp.name == "input":
                continue
            shape = [1 if not isinstance(d, int) else d for d in inp.shape]
            self._states[inp.name] = np.zeros(shape, dtype=np.float32)
        self.reset()

    def reset(self):
        for name in self._states:
            if name != "sr":
                self._states[name] = np.zeros_like(self._states[name])
        self._context = np.zeros(self.CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        if frame.size != FRAME_SAMPLES:
            raise ValueError(f"SileroVad wants {FRAME_SAMPLES}-sample frames")
        audio = np.concatenate([self._context, frame])  # 576 = model's input width
        out = self.session.run(None, {"input": audio.reshape(1, -1), **self._states})
        probs, *states = out
        for name, value in zip([n for n in self._states if n != "sr"], states):
            self._states[name] = np.asarray(value, dtype=np.float32)
        self._context = frame[-self.CONTEXT_SAMPLES:]
        return float(np.asarray(probs).reshape(-1)[-1])


class EnergyVad:
    """Adaptive energy gate: noise floor + hysteresis. No models, no deps.

    The floor follows the quietest recent audio (it learns the room). A frame
    is speech when its RMS clears floor * open_mult; once open, the gate stays
    open until RMS drops under floor * close_mult (that gap is the hysteresis
    — it stops the decision from chattering on borderline audio).
    """

    def __init__(self, open_mult: float = 3.0, close_mult: float = 1.8,
                 floor_alpha: float = 0.02, floor_init: float = 1e-3):
        self.open_mult = open_mult
        self.close_mult = close_mult
        self.floor_alpha = floor_alpha
        self.floor = max(floor_init, 1e-9)
        self._open = False

    def reset(self):
        self._open = False

    def __call__(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        rms = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        if rms >= self.floor * self.open_mult:
            self._open = True
        elif self._open:
            if rms <= self.floor * self.close_mult:
                self._open = False
        else:
            # learn the room only on frames that are neither opening nor
            # holding the gate — else the burst itself lifts the floor
            self.floor += self.floor_alpha * (rms - self.floor)
            self.floor = max(self.floor, 1e-9)
        return 1.0 if self._open else 0.0


def make_vad():
    """(vad, backend_name): Silero when its package is installed, energy otherwise."""
    try:
        return SileroVad(), "silero"
    except Exception as e:  # missing faster_whisper/onnxruntime/model file
        print(f"[listen] Silero VAD unavailable ({type(e).__name__}: {e}); "
              "using the energy fallback")
        return EnergyVad(), "energy"


@dataclass
class SegmentEnd:
    """One finished utterance. audio=None means it was dropped (not enough
    real speech in it — a cough blip or a door)."""

    audio: np.ndarray | None
    seconds: float
    voiced_seconds: float


class Segmenter:
    """Frame probabilities in, whole utterances out. Pure: feed(frame, prob)."""

    def __init__(self, threshold: float = DEFAULTS["threshold"],
                 min_speech_ms: float = DEFAULTS["min_speech_ms"],
                 silence_ms: float = DEFAULTS["silence_ms"],
                 pre_roll_ms: float = DEFAULTS["pre_roll_ms"],
                 max_seconds: float = DEFAULTS["max_seconds"],
                 min_voiced_ms: float = DEFAULTS["min_voiced_ms"]):
        self.threshold = float(threshold)
        self.need_speech = frame_count(min_speech_ms)
        self.need_silence = frame_count(silence_ms)
        self.pre_roll = frame_count(pre_roll_ms)
        self.max_frames = max(self.need_speech + 1, frame_count(max_seconds * 1000.0))
        self.min_voiced = frame_count(min_voiced_ms)
        self.reset()

    def reset(self):
        """Forget everything mid-stream (she started speaking: drop frames)."""
        # the ring must hold pre_roll_ms of audio from BEFORE a pending speech
        # streak as well as the streak itself, or pre-roll can't reach back
        self._keep = self.pre_roll + max(self.need_speech, 1)
        self._ring = deque(maxlen=self._keep)       # recent audio, while idle
        self._buf: list[np.ndarray] = []            # frames of the open segment
        self._pending = 0                           # voiced frames before start
        self._pending_ring: deque = deque()         # their audio (start point)
        self._voiced = 0
        self._silence = 0
        self._active = False
        self._preroll_frames = 0

    def push(self, frame: np.ndarray, prob: float) -> SegmentEnd | None:
        voiced = prob > self.threshold
        if not self._active:
            self._ring.append(frame)  # always keep pre_roll_ms of history
            if voiced:
                self._pending += 1
                self._pending_ring.append(frame)
                while len(self._pending_ring) > self.need_speech:
                    self._pending_ring.popleft()
                if self._pending >= self.need_speech:
                    # speech confirmed: open the segment, back-dated by pre_roll
                    back = self._pending_ring  # oldest-first, the speech frames
                    self._buf = list(self._ring)
                    # drop pre_roll + speech frames from the ring tail that are
                    # already in _buf so the segment audio has no duplicates
                    overlap = len(back)
                    if self.pre_roll == 0:
                        self._buf = []
                    elif len(self._buf) > overlap:
                        self._buf = self._buf[: len(self._buf) - overlap]
                    self._buf.extend(back)
                    self._preroll_frames = len(self._buf) - overlap
                    self._active = True
                    self._voiced = overlap
                    self._silence = 0
                    self._pending = 0
                    self._pending_ring.clear()
                    self._ring.clear()
            else:
                self._pending = 0
                self._pending_ring.clear()
            return None

        self._buf.append(frame)
        if voiced:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self.need_silence or len(self._buf) >= self.max_frames:
            return self._finish()
        return None

    def _finish(self) -> SegmentEnd:
        audio = np.concatenate(self._buf) if self._buf else np.zeros(0, np.float32)
        voiced_s = self._voiced * FRAME_MS / 1000.0
        total_s = len(audio) / SAMPLE_RATE
        dropped = self._voiced < self.min_voiced
        self.reset()
        return SegmentEnd(None if dropped else audio, total_s, voiced_s)
