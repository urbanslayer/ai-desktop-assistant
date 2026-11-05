"""Her ears: record your voice (push-to-talk) and turn it into text.

asr: remote -> Faster-Whisper on another machine (server/asr_server.py, or any
               OpenAI-compatible /v1/audio/transcriptions server). The mic is
               always the one on this PC; only the WAV clip goes over the network.
asr: local  -> Faster-Whisper on this PC
asr: groq   -> Groq's online Whisper
"""
import difflib
import io
import os
import queue
import re
import threading
import time
from collections import deque

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
    by the VAD in riko/vad.py. Each finished segment goes to
    on_segment(wav, stats) — the SAME path push-to-talk uses
    (submit_audio -> transcribe -> turn), tagged source='vad' so the turn can
    tell VAD noise from a deliberate clip. stats carries the Silero evidence
    the noise filter weighs: seconds / voiced_seconds / mean_prob.

    Deliberately silent to the UI: the point of always mode is that there is
    no listening indicator, so this never emits a 'listening' status.
    pause()/resume() ignore frames and forget the half-built segment (she is
    speaking, or a turn is thinking); the mic stream STAYS OPEN the whole
    time and every frame still goes through the VAD, so its recurrent state
    never goes stale across the pause. Between tail() (her voice really
    ended) and resume() the frames are kept in a short rolling pre-roll: the
    next segment can back-date into them and the syllables you said during
    the echo tail are not lost. Frames captured while she was still audible
    (before tail()) are never fed into a segment. suspend()/release() hand
    the mic device itself to the push-to-talk Recorder while Space is held.
    A device error mid-stream retries with backoff; start() raises so the
    caller can fall back to push-to-talk.

    Stream ownership (the owner's L-key crash: pressing L closed the
    PortAudio stream from the Qt thread while the reader thread was inside a
    blocking stream.read() — undefined behaviour, SIGSEGV/SIGABRT, the whole
    app died with no traceback): exactly ONE thread, the reader, opens,
    reads, stops and closes the stream. The stream runs in sounddevice
    CALLBACK mode — PortAudio's own thread only enqueues frames into a
    queue — so the reader never sits inside PortAudio: it pulls from the
    queue with a short timeout and notices close()/suspend() requests
    between frames (tens of ms). Those setters only set events; close()
    joins the reader (which closes its own stream on the way out) and
    suspend() waits on an Event the reader sets once the device is
    released — both bounded, so a stuck device delays, never hangs. A
    device that stops delivering frames entirely (stall_seconds) is treated
    as gone: the reader closes its stream and reopens with backoff.
    """

    CLOSE_TIMEOUT = 1.5   # how long close()/suspend() wait for the reader

    def __init__(self, on_segment, cfg=None, sd_module=None, vad=None,
                 retry_delay=0.5, retry_max=10.0, device=None,
                 stall_seconds=2.0, poll_interval=0.02):
        self.on_segment = on_segment
        self._sd = sd_module or sd
        self._cfg = dict(vad_mod.DEFAULTS, **(cfg or {}))
        self._vad = vad          # None -> backend chosen at start()
        self.vad_name = getattr(vad, "name", "injected") if vad else None
        self._segmenter = None
        self._thread = None
        self._stop = threading.Event()
        self._pause = threading.Event()      # frames ignored, stream stays open
        self._suspend = threading.Event()    # device handed to the Recorder
        self._resume = threading.Event()     # released / resumed, loop wakes up
        self._idle = threading.Event()       # reader holds NO stream right now
        self._idle.set()
        self._started = threading.Event()    # first open attempt finished
        self._start_error = None             # ... and what it raised, if anything
        self._device = device                # sounddevice device (None = default)
        self._stall_seconds = float(stall_seconds)
        self._poll = float(poll_interval)
        self._retry_delay = retry_delay
        self._retry_max = retry_max
        self._lock = threading.Lock()
        # rolling audio captured while paused; (frame, kept) — kept=True only
        # after tail(), i.e. only once her own voice is really over.
        self._tail_ring: deque = deque(maxlen=max(1, vad_mod.frame_count(self._cfg["pre_roll_ms"])))
        self._tail_kept = False
        self._seed: list | None = None       # tail frames for the reader to prime

    @property
    def running(self):
        return self._thread is not None

    def start(self):
        """Start the reader thread, which opens the stream; raises if the mic
        won't open (the first open attempt is reported back here so the
        caller can fall back to push-to-talk)."""
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
            self._started.clear()
            self._start_error = None
            thread = threading.Thread(target=self._run, daemon=True,
                                      name="riko-vad")
            self._thread = thread
            thread.start()
        if not self._started.wait(timeout=10.0):
            # the device open itself hung: nothing was opened we must close
            self._stop.set()
            self._resume.set()
            thread.join(timeout=1.0)
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            raise OSError("opening the mic stream timed out")
        if self._start_error is not None:
            err, self._start_error = self._start_error, None
            thread.join(timeout=1.0)
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            raise err

    def _open_stream(self, frames: queue.Queue):
        """Open + start the capture stream in CALLBACK mode: PortAudio's own
        thread calls `callback`, which only enqueues. The stream belongs to
        the calling reader thread from here until that same thread closes
        it. (A blocking stream.read() is what made a close() from another
        thread crash the process — there is no read left to block in.)"""

        def callback(indata, count, time_info, status):  # PortAudio's thread
            frames.put(np.asarray(indata).copy())

        stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                      dtype="float32", device=self._device,
                                      callback=callback)
        try:
            stream.start()  # no-op with a callback; kept for parity with Recorder
        except Exception:
            stream.close()  # same thread that just opened it, nothing reading
            raise
        return stream

    def close(self):
        """Stop the reader and release the device (shutdown).

        Never touches the stream from this thread: only the reader thread
        stops/closes it (closing from here while the reader is inside
        PortAudio aborts the process — no Python traceback, the app 'just
        closes'). We set the events and wait, bounded, for the reader to
        dismantle its own stream and exit."""
        with self._lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        self._resume.set()  # wake a suspended reader so join is quick
        if thread is not None:
            thread.join(timeout=self.CLOSE_TIMEOUT)
            if thread.is_alive():
                print("[listen] the mic reader is still finishing; "
                      "it will close its stream on its own")

    def pause(self):
        """Ignore frames and forget the half-built segment (she is speaking).
        The stream stays open and the VAD keeps seeing every frame."""
        self._pause.set()
        with self._lock:
            self._tail_ring.clear()
            self._tail_kept = False
        self._seed = None  # a new hold cancels any unclaimed tail pre-roll

    def tail(self):
        """Her voice really just ended (the page said so). Audio from here on
        is echo-era, not her: resume() may pre-roll the next segment back
        into it so the first syllables of your reply are not lost."""
        with self._lock:
            self._tail_kept = True

    def resume(self):
        # Hand the tail frames to the reader thread; it primes the segmenter
        # before the next push so pre-roll can cross the pause boundary.
        with self._lock:
            seed = [f for f, keep in self._tail_ring if keep]
            self._tail_ring.clear()
            self._tail_kept = False
        self._seed = seed or None
        self._pause.clear()

    def suspend(self):
        """Give the mic device to the push-to-talk Recorder (Space held).

        Waits (bounded) for the reader to close its own stream, so the
        Recorder cannot open a second capture on a device the VAD has not
        released yet."""
        self._suspend.set()
        if not self._idle.wait(timeout=self.CLOSE_TIMEOUT):
            print("[listen] the mic stream did not release in time; "
                  "the recorder may find the device busy")

    def release(self):
        if self._suspend.is_set():
            self._suspend.clear()
            self._resume.set()

    # ── the reader thread: the stream's ONLY owner ──────────
    def _run(self):
        delay = self._retry_delay
        stream = None
        frames: queue.Queue = queue.Queue()
        carry = np.zeros(0, dtype=np.float32)  # partial frame between callbacks
        last_frame = 0.0
        first_open = True
        try:
            while not self._stop.is_set():
                if stream is None or self._suspend.is_set():
                    if stream is not None:
                        self._close_quiet(stream)  # the opener closes: always safe
                        stream = None
                        carry = np.zeros(0, dtype=np.float32)
                        self._idle.set()
                    if self._suspend.is_set():     # the Recorder owns the device
                        self._resume.wait(timeout=self._poll)
                        continue
                    try:
                        stream = self._open_stream(frames)
                    except Exception as e:
                        if first_open:             # dead mic: let start() raise
                            self._start_error = e
                            self._started.set()
                            return
                        print(f"[listen] mic stream failed ({type(e).__name__}: {e}); "
                              f"retrying in {delay:.1f}s")
                        if self._stop.wait(delay) or self._suspend.is_set():
                            delay = self._retry_delay
                        else:
                            delay = min(delay * 2, self._retry_max)
                        continue
                    first_open = False
                    self._start_error = None
                    self._started.set()
                    self._idle.clear()
                    delay = self._retry_delay
                    last_frame = time.monotonic()
                    while True:  # drop anything the previous stream had queued
                        try:
                            frames.get_nowait()
                        except queue.Empty:
                            break
                    print("[listen] mic stream back up")
                    continue
                try:
                    data = frames.get(timeout=self._poll)
                except queue.Empty:
                    if time.monotonic() - last_frame <= self._stall_seconds:
                        continue  # between callbacks: check the events, poll again
                    data = TimeoutError(f"no audio frames for {self._stall_seconds:.1f}s")
                if isinstance(data, BaseException):
                    # device gone: close OUR stream here, back off, reopen
                    self._close_quiet(stream)
                    stream = None
                    carry = np.zeros(0, dtype=np.float32)
                    self._idle.set()
                    print(f"[listen] mic stream failed ({type(data).__name__}: {data}); "
                          f"retrying in {delay:.1f}s")
                    if self._stop.wait(delay) or self._suspend.is_set():
                        delay = self._retry_delay
                    else:
                        delay = min(delay * 2, self._retry_max)
                    continue
                last_frame = time.monotonic()
                chunk = np.asarray(data, dtype=np.float32).reshape(-1)
                carry = np.concatenate((carry, chunk)) if carry.size else chunk
                while carry.size >= vad_mod.FRAME_SAMPLES and not self._stop.is_set():
                    frame = carry[:vad_mod.FRAME_SAMPLES]
                    carry = carry[vad_mod.FRAME_SAMPLES:]
                    self._handle_frame(frame)
        finally:
            # shutdown with a stream still in hand (close() during a read
            # wait — the L key): release the device HERE, on the thread that
            # opened it, before the thread ends.
            if stream is not None:
                self._close_quiet(stream)
                self._idle.set()

    def _handle_frame(self, frame):
        # Every frame feeds the VAD, even while paused: Silero is
        # recurrent, and keeping it warm across her turn is exactly what
        # lets the first frames after resume read true speech at once.
        prob = float(self._vad(frame))
        if self._pause.is_set() or self._suspend.is_set():
            if self._pause.is_set():  # rolling echo-era audio, not her voice
                with self._lock:
                    self._tail_ring.append((np.asarray(frame).reshape(-1),
                                            self._tail_kept))
            self._segmenter.reset()
            return
        if self._seed:
            self._segmenter.prime(self._seed)
            self._seed = None
        end = self._segmenter.push(np.asarray(frame).reshape(-1), prob)
        if end is None:
            return
        if end.audio is None:
            print(f"[asr] always-listen: dropped a {end.seconds:.1f}s blip "
                  f"({end.voiced_seconds:.2f}s of speech)")
            return
        wav = io.BytesIO()
        sf.write(wav, end.audio, SAMPLE_RATE, format="WAV")
        print(f"[asr] always-listen: caught {end.seconds:.1f}s")
        stats = {"seconds": end.seconds, "voiced_seconds": end.voiced_seconds,
                 "mean_prob": end.mean_prob}
        try:
            self.on_segment(wav.getvalue(), stats)
        except Exception as e:  # a segment must not kill the reader
            print(f"[asr] always-listen: segment dropped ({type(e).__name__}: {e})")

    def _close_quiet(self, stream):
        """Stop + close a stream. Call ONLY from the thread that opened it."""
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


# ── getting the names right ──────────────────────────────────
# Whisper turns 'Furina' into 'Farina' when it has never seen the word.
# Two layers fix that:
#  1. a short vocabulary prompt (initial_prompt) rides along with every
#     clip: Whisper biases the SPELLING of words it sees there;
#  2. deterministic fix-ups on the finished text: an exact corrections
#     table plus a conservative fuzzy pass over capitalised near-misses.
ASR_PROMPT_MAX = 200               # Whisper truncates initial_prompt at 224 tokens anyway
FUZZY_MIN_RATIO = 0.8              # SequenceMatcher ratio a near-miss must reach
FUZZY_MIN_LEN = 4                  # shorter words fuzzy-match too easily

# Names the app always knows (the owner's persona and friends); hub
# persona names are merged in at runtime, and `asr.vocabulary:` in
# character_config.yaml adds the rest.
DEFAULT_VOCABULARY = ("Furina", "Riko", "senpai", "Starburst 1200")

# Exact fix-ups: a misheard spelling -> the right word. Case-insensitive,
# whole-word. `asr.corrections:` in character_config.yaml merges on top of
# this (an identity entry like `Farina: Farina` switches one off).
DEFAULT_CORRECTIONS = {
    "Farina": "Furina", "Fareena": "Furina",
    "Furena": "Furina", "Fureena": "Furina",
}

# Real English words the fuzzy pass must never rewrite, however close they
# look to a vocabulary word (Marina vs Furina). The exact corrections table
# still applies to them — that one is an explicit statement that the word
# was misheard; `asr.corrections` can override it (see above).
COMMON_WORD_GUARD = frozenset("""
farina marina arena sienna fiona flora fauna piranha senora senor amigo
forma formula family famous finally friend banana pizza pasta salsa ninja
rina rico miko
""".split())


def _asr_opt(cfg, name, default):
    """Read an `asr.<name>` option: the dotted top-level yaml key
    (`asr.vocabulary:` next to `asr: remote`) or, if `asr:` were ever a
    mapping, `asr: {<name>: ...}`."""
    value = cfg.get(f"asr.{name}")
    if value is None and isinstance(cfg.get("asr"), dict):
        value = cfg["asr"].get(name)
    return default if value is None else value


def asr_vocabulary(cfg):
    """Words Whisper should know: active persona first, the hub's other
    persona names, the built-in defaults, then `asr.vocabulary:` from the
    config. Deduped case-insensitively, order preserved. Never raises: a
    hub that is down just contributes no names."""
    words = []
    try:
        from riko import hub_client
        words += hub_client.persona_names()
    except Exception:  # no hub client (terminal use), hub down, bad data
        pass
    words += list(DEFAULT_VOCABULARY)
    extra = _asr_opt(cfg, "vocabulary", [])
    if isinstance(extra, str):
        extra = [extra]
    try:
        words += [str(w) for w in extra]
    except TypeError:  # not a list at all: ignore the config typo
        pass
    out, seen = [], set()
    for w in words:
        w = str(w).strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def asr_prompt(cfg):
    """The initial_prompt sentence: 'Riko, Furina, Asuka, senpai.' capped
    at ASR_PROMPT_MAX chars (words are dropped from the end, never cut
    mid-word). '' = send no prompt."""
    out = ""
    for w in asr_vocabulary(cfg):
        cand = f"{out}, {w}" if out else w
        if len(cand) + 1 > ASR_PROMPT_MAX:  # +1 for the closing '.'
            break
        out = cand
    return f"{out}." if out else ""


def _match_case(source, target):
    """Spell `target` the way `source` was spelled (Farina->Furina,
    farina->furina, FARINA->FURINA)."""
    if source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    if source.islower():
        return target.lower()
    return target


def _correction_table(cfg):
    table = dict(DEFAULT_CORRECTIONS)
    user = _asr_opt(cfg, "corrections", {})
    if isinstance(user, dict):
        for bad, good in user.items():
            bad, good = str(bad).strip(), str(good).strip()
            if bad and good:
                table[bad] = good
    return {k: v for k, v in table.items() if k.lower() != v.lower()}


def _jaro_winkler(a: str, b: str) -> float:
    """Plain Jaro-Winkler (Winkler prefix bonus 0.1, prefix capped at 4)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(len(a), len(b)) // 2 - 1
    a_hit = [False] * len(a)
    b_hit = [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - window), min(i + window + 1, len(b))):
            if not b_hit[j] and b[j] == ch:
                a_hit[i] = b_hit[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    k = transposes = 0
    for i in range(len(a)):
        if a_hit[i]:
            while not b_hit[k]:
                k += 1
            if a[i] != b[k]:
                transposes += 1
            k += 1
    transposes //= 2
    jaro = (matches / len(a) + matches / len(b)
            + (matches - transposes) / matches) / 3
    prefix = 0
    for i in range(min(4, len(a), len(b))):
        if a[i] != b[i]:
            break
        prefix += 1
    return jaro + 0.1 * prefix * (1 - jaro)


def _similar(a: str, b: str) -> float:
    """Similarity of two lowercase words, 0..1: the better of difflib's ratio
    and Jaro-Winkler. difflib alone misses phonetic spellings whose shared
    letters split into blocks ('Asooka'/'asuka' = 0.73, two edits that should
    count); Jaro-Winkler catches them (0.86) while staying under 0.8 on the
    non-names ('Farm'/'furina' 0.61, 'Marina'/'furina' 0.78)."""
    return max(difflib.SequenceMatcher(None, a, b).ratio(), _jaro_winkler(a, b))


def _fuzzy_targets(cfg):
    """Targets for the fuzzy pass: whole vocabulary entries plus the
    alphabetic words inside multi-word entries ('Starburst 1200' -> also
    'Starburst'). Only name-like words (length >= FUZZY_MIN_LEN)."""
    targets, seen = [], set()
    for entry in asr_vocabulary(cfg):
        parts = [entry] + [p for p in entry.split() if len(p) >= FUZZY_MIN_LEN]
        for p in parts:
            if len(p) >= FUZZY_MIN_LEN and p.lower() not in seen:
                seen.add(p.lower())
                targets.append(p)
    return targets


def apply_corrections(text, cfg):
    """Fix words Whisper misheard, and log every fix ('[asr] corrected
    Farina -> Furina'). The corrected text is what gets shown and sent.

    Pass 1: the exact corrections table (case-insensitive, whole-word).
    Pass 2: capitalised words (name-shaped) fuzzy-match a vocabulary word —
    ratio >= 0.8, length >= 4, real English words (COMMON_WORD_GUARD) and
    vocabulary words themselves are never touched. Deliberately
    conservative: 'Marina' and 'Farm' stay, 'Asooka' becomes 'Asuka'."""
    if not text:
        return text
    out = text
    for bad, good in _correction_table(cfg).items():
        pattern = re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE)

        def _sub(m, good=good):
            fixed = _match_case(m.group(0), good)
            if fixed != m.group(0):
                print(f"[asr] corrected {m.group(0)} -> {fixed}")
            return fixed

        out = pattern.sub(_sub, out)
    targets = _fuzzy_targets(cfg)
    target_lowers = {t.lower() for t in targets}

    def _fuzzy(m):
        word = m.group(0)
        low = word.lower()
        if low in target_lowers or low in COMMON_WORD_GUARD:
            return word
        best, ratio = None, 0.0
        for t in targets:
            r = _similar(low, t.lower())
            if r > ratio:
                best, ratio = t, r
        if best and ratio >= FUZZY_MIN_RATIO:
            fixed = _match_case(word, best)
            if fixed != word:
                print(f"[asr] corrected {word} -> {fixed} "
                      f"(fuzzy {ratio:.2f})")
                return fixed
        return word

    return re.sub(r"\b[A-Z][A-Za-z'’-]{3,}\b", _fuzzy, out)


# ── what counts as "Whisper invented that" ───────────────────
# No phrase blocklist: "Thank you." is a real sentence a person says. A VAD
# segment is only dropped when the evidence says nobody spoke:
#  - Whisper's own per-segment verdicts (verbose_json): every segment looks
#    like non-speech, or a segment has the rambly-repetition shape, or
#  - Silero never truly believed the voiced frames (their mean probability
#    hovers at the open threshold — TV noise, not a voice).
# Without Whisper metrics (an ASR server too old for verbose_json fields)
# Silero alone has to vouch for the clip, so it demands stronger proof.
NO_SPEECH_PROB_MAX = 0.6         # above: Whisper thinks there is no speech in it
AVG_LOGPROB_MIN = -1.0           # below: Whisper was guessing at the words
COMPRESSION_RATIO_MAX = 2.4      # above: repetitive filler, the hallucination shape
SILERO_MIN_MEAN_PROB = 0.6       # with Whisper metrics: drop below this
SILERO_FALLBACK_MIN_MEAN_PROB = 0.7  # without metrics: demand a confident voice
FALLBACK_MIN_VOICED_SECONDS = 0.4    # ... over at least this much real speech


def _empty_reason(text):
    t = (text or "").strip()
    if not t:
        return "no words"
    if not any(c.isalnum() for c in t):
        return "punctuation only"
    return None


def hallucination_reason(text, wav_bytes=None, noise_phrases=True):
    """Why this transcript is nothing: a reason str, or None when there are
    words. Empty / punctuation-only transcripts always count.

    The classic Whisper phrase blocklist ("Thank you.", "Thanks for
    watching!") is GONE: it ate real thanks said to her. VAD segments are
    judged by evidence instead — see vad_noise_reason. wav_bytes and
    noise_phrases are accepted but unused (old call sites)."""
    return _empty_reason(text)


def vad_noise_reason(text, evidence, vad_stats=None):
    """Drop this VAD segment (reason str) or accept it (None), on evidence:

    evidence = {"segments": [{"no_speech_prob", "avg_logprob",
              "compression_ratio"}, ...]} from Whisper itself (verbose_json),
    or None/{} when the backend has no such fields. vad_stats = the Silero
    numbers the segmenter measured: voiced_seconds / mean_prob (mean over
    the voiced frames).

    With Whisper metrics they decide: a segment is noise only when Whisper
    calls ALL of its segments non-speech (no_speech_prob high AND avg_logprob
    low), or one has the repetitive-filler shape (compression_ratio high), or
    Silero never believed the voice. Anything else passes — including a real
    'Thank you.' / 'Bye.'. Without metrics, accept only when Silero vouches:
    >= 0.4 s of voiced audio with mean probability >= 0.7."""
    reason = _empty_reason(text)
    if reason:
        return reason
    st = vad_stats or {}
    try:
        voiced = float(st.get("voiced_seconds") or 0.0)
        mean_p = float(st.get("mean_prob") or 0.0)
    except (TypeError, ValueError):
        voiced = mean_p = 0.0
    segs = [s for s in ((evidence or {}).get("segments") or []) if isinstance(s, dict)]
    if segs:
        if all(float(s.get("no_speech_prob") or 0.0) > NO_SPEECH_PROB_MAX
               and float(s.get("avg_logprob") or 0.0) < AVG_LOGPROB_MIN
               for s in segs):
            return "whisper says non-speech"
        if any(float(s.get("compression_ratio") or 0.0) > COMPRESSION_RATIO_MAX
               for s in segs):
            return "rambly repetition (compression ratio)"
        if mean_p and mean_p < SILERO_MIN_MEAN_PROB:
            return f"weak silero evidence (mean prob {mean_p:.2f})"
        return None
    # No Whisper metrics: trust Silero alone, but strictly.
    if voiced >= FALLBACK_MIN_VOICED_SECONDS and mean_p >= SILERO_FALLBACK_MIN_MEAN_PROB:
        return None
    return (f"no whisper metrics; weak silero evidence "
            f"({voiced:.2f}s voiced @ mean prob {mean_p:.2f})")


def _segments_evidence(segs) -> dict:
    """Normalise whisper-SDK / verbose_json segment objects into plain dicts
    of the three fields vad_noise_reason weighs."""
    out = []
    for s in segs:
        get = s.get if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
        entry = {"no_speech_prob": get("no_speech_prob"),
                 "avg_logprob": get("avg_logprob"),
                 "compression_ratio": get("compression_ratio")}
        if any(v is None for v in entry.values()):
            continue
        out.append({k: float(v) for k, v in entry.items()})
    return {"segments": out}


def transcribe(wav_bytes, cfg):
    mode = cfg["asr"]
    prompt = asr_prompt(cfg)  # vocabulary bias: 'Riko, Furina, senpai.'
    if mode == "groq":
        groq = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
        extra: dict = {"prompt": prompt} if prompt else {}
        text = groq.audio.transcriptions.create(model=GROQ_MODEL, file=("you.wav", wav_bytes),
                                                **extra).text
    elif mode == "remote":
        if _remote is None:
            raise RuntimeError("remote ASR was never set up: call asr.check(cfg) first "
                               "(it failed? start server/asr_server.py on that machine, or set asr: local)")
        r = cfg["remote_asr"]
        extra: dict = {"language": r["language"]} if r.get("language") else {}
        if prompt:
            extra["prompt"] = prompt  # asr_server.py passes it to initial_prompt
        text = _remote.audio.transcriptions.create(model=r.get("model", "whisper-1"),
                                                   file=("you.wav", wav_bytes), **extra).text
    else:
        if _whisper is None:
            raise RuntimeError("local ASR was never loaded: call asr.check(cfg) first")
        segments, _ = _whisper.transcribe(io.BytesIO(wav_bytes),
                                          initial_prompt=prompt or None)
        text = "".join(s.text for s in segments)  # each segment already starts with a space
    return text.strip()


def transcribe_verbose(wav_bytes, cfg):
    """transcribe() for VAD segments: (text, evidence) where evidence is
    {"segments": [{"no_speech_prob", "avg_logprob", "compression_ratio"},...]}
    — Whisper's own confidence about what it just wrote — or None when the
    backend has no verbose_json fields (an ASR server from before segments
    were added; the caller then falls back to the Silero-only rule)."""
    mode = cfg["asr"]
    prompt = asr_prompt(cfg)  # same vocabulary bias as transcribe()
    try:
        if mode == "local":
            if _whisper is None:
                raise RuntimeError("local ASR was never loaded: call asr.check(cfg) first")
            segments, _ = _whisper.transcribe(io.BytesIO(wav_bytes),
                                              initial_prompt=prompt or None)
            text, segs = [], []
            for s in segments:  # the generator must be drained inside no lock here
                text.append(s.text)
                segs.append(s)
            return "".join(text).strip(), _segments_evidence(segs)
        if mode == "groq":
            client = OpenAI(api_key=os.getenv("GROQ_API_KEY"),
                            base_url="https://api.groq.com/openai/v1")
            model, extra = GROQ_MODEL, {}
        else:
            if _remote is None:
                raise RuntimeError("remote ASR was never set up: call asr.check(cfg) first")
            client, r = _remote, cfg["remote_asr"]
            model = r.get("model", "whisper-1")
            extra = {"language": r["language"]} if r.get("language") else {}
        if prompt:
            extra["prompt"] = prompt
        resp = client.audio.transcriptions.create(
            model=model, file=("you.wav", wav_bytes),
            response_format="verbose_json", **extra)
        text = (getattr(resp, "text", "") or "").strip()
        raw = getattr(resp, "segments", None)
        if raw is None:  # old server: verbose_json without segments
            return text, None
        evidence = _segments_evidence(raw)
        if not evidence["segments"]:
            return text, None  # a real transcription with no usable segments
        return text, evidence
    except Exception as e:
        # Any failure of the verbose call: plain transcription is still fine,
        # the caller just judges the clip on Silero evidence alone.
        print(f"[asr] verbose_json unavailable ({type(e).__name__}: {e}); "
              "using the plain response")
        return transcribe(wav_bytes, cfg), None


if __name__ == "__main__":
    # Talk into your mic and see what she hears: uv run python -m riko.asr
    from riko.config import load_config
    cfg = load_config()
    check(cfg)
    clip = record()
    print("You said:", transcribe(clip, cfg) if clip else "(nothing)")
