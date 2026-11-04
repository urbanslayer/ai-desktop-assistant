"""Listening-mode tests: VAD segmentation, AlwaysListener, the hallucination
filter, always-vs-push-to-talk behaviour, toggling and persistence.

Synthetic audio arrays + fake sounddevice + fake hub: no mic, no GPU, no
network beyond localhost. Nothing here ever opens a real audio device.
"""
import importlib.util
import os
import queue
import time
from pathlib import Path

import numpy as np
import pytest
import requests
from conftest import free_port, make_cfg, wav_bytes

from riko import appstate, asr, hub_client, session, vad, viewer
from riko import vad as vad_mod
from riko.asr import AlwaysListener as RealAlwaysListener  # survive fixture patching

SR = vad.SAMPLE_RATE
N = vad.FRAME_SAMPLES


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


# ── fake audio world ──────────────────────────────────────────
class FakeStream:
    """Pull-based stand-in for sd.InputStream (AlwaysListener.read). A real mic
    always has data; so this returns silence on an empty queue instead of
    blocking forever, or the reader could never notice a suspend/close."""

    def __init__(self, sd):
        self.sd = sd
        self.stopped = False
        self.closed = False

    def start(self):
        pass

    def read(self, n):
        if self.sd.read_error:
            raise RuntimeError("device disappeared")
        try:
            item = self.sd.frames.get(timeout=0.02)
        except queue.Empty:
            item = None  # room tone: a live stream never starves like this
        if item is None:
            return np.zeros(n, np.float32), False
        return item, False

    def stop(self):
        self.stopped = True
        self.sd.frames.put(None)

    def close(self):
        self.closed = True


class FakeSD:
    def __init__(self, fail_open=False):
        self.streams = []
        self.fail_open = fail_open
        self.read_error = False
        self.frames = queue.Queue()

    def InputStream(self, **kwargs):  # noqa: N802 (sounddevice API)
        if self.fail_open:
            raise OSError("no such input device")
        stream = FakeStream(self)
        self.streams.append(stream)
        return stream


def put_frames(sd, idle=0, loud=0, quiet=0):
    for _ in range(idle):
        sd.frames.put(np.full(N, 0.001, np.float32))
    for _ in range(loud):
        sd.frames.put(np.full(N, 0.05, np.float32))
    for _ in range(quiet):
        sd.frames.put(np.full(N, 0.001, np.float32))


class FakeListener:
    """Same surface as asr.AlwaysListener: counters instead of threads/mic.
    `calls` records pause/resume/tail in order so timing-order tests can see
    the resume path (tail before resume, no idle-status in between)."""

    def __init__(self, on_segment, cfg=None):
        self.on_segment = on_segment
        self.cfg = cfg or {}
        self.started = 0
        self.closed_count = 0
        self.pause_calls = self.resume_calls = 0
        self.suspended = self.released = 0
        self.calls = []

    def start(self):
        self.started += 1

    def close(self):
        self.closed_count += 1

    def pause(self):
        self.pause_calls += 1
        self.calls.append("pause")

    def resume(self):
        self.resume_calls += 1
        self.calls.append("resume")

    def tail(self):
        self.calls.append("tail")

    def suspend(self):
        self.suspended += 1

    def release(self):
        self.released += 1

    def feed(self, wav, stats=None):  # test hook: pretend the VAD finished an utterance
        self.on_segment(wav, stats)


def collector(segs, stats_box=None):
    """on_segment(wav, stats) shaped sink for the real AlwaysListener tests."""
    def sink(wav, stats=None):
        segs.append(wav)
        if stats_box is not None:
            stats_box.append(stats)
    return sink


# ── Segmenter: pure segmentation on synthetic frames ─────────
def test_silence_yields_nothing():
    seg = vad.Segmenter()
    assert all(seg.push(np.zeros(N, np.float32), 0.0) is None for _ in range(200))


def test_speech_opens_after_min_speech_with_preroll():
    seg = vad.Segmenter()  # defaults: speech>=7f, silence>=25f, pre-roll 10f, voiced>=15f
    ends = []
    for i in range(10):  # distinct idle frames so pre-roll is recognisable
        ends.append(seg.push(np.full(N, float(i + 1), np.float32), 0.0))
    for _ in range(20):
        ends.append(seg.push(np.full(N, 100.0, np.float32), 0.9))
    for _ in range(25):
        ends.append(seg.push(np.zeros(N, np.float32), 0.0))
    assert sum(e is not None for e in ends) == 1
    end = next(e for e in ends if e is not None)
    assert end.audio is not None and end.voiced_seconds > 0.5
    assert len(end.audio) == (10 + 20 + 25) * N
    np.testing.assert_allclose(end.audio[:10 * N], np.arange(1, 11, dtype=np.float32).repeat(N))
    np.testing.assert_allclose(end.audio[10 * N:30 * N], 100.0)


def test_blip_below_min_speech_never_opens():
    seg = vad.Segmenter()
    ends = [seg.push(np.ones(N, np.float32), 0.9) for _ in range(6)]  # 6 < 7 frames
    ends += [seg.push(np.zeros(N, np.float32), 0.0) for _ in range(100)]
    assert all(e is None for e in ends)


def test_short_voiced_segment_is_dropped():
    seg = vad.Segmenter()
    ends = [seg.push(np.ones(N, np.float32), 0.9) for _ in range(8)]  # 0.26 s voiced
    ends += [seg.push(np.zeros(N, np.float32), 0.0) for _ in range(30)]
    end = next(e for e in ends if e is not None)
    assert end.audio is None
    assert 0 < end.voiced_seconds < 0.5


def test_short_gap_does_not_end_the_segment():
    seg = vad.Segmenter()
    ends = []
    for _ in range(15):
        ends.append(seg.push(np.ones(N, np.float32), 0.9))
    for _ in range(10):  # 0.32 s of silence: less than silence_ms=800
        ends.append(seg.push(np.zeros(N, np.float32), 0.0))
    for _ in range(10):
        ends.append(seg.push(np.ones(N, np.float32), 0.9))
    for _ in range(30):
        ends.append(seg.push(np.zeros(N, np.float32), 0.0))
    assert sum(e is not None for e in ends) == 1
    end = next(e for e in ends if e is not None)
    assert end.audio is not None and end.seconds > 1.5


def test_max_seconds_caps_the_utterance():
    seg = vad.Segmenter(max_seconds=1.0)
    end = None
    for _ in range(200):  # someone talks forever
        end = seg.push(np.ones(N, np.float32), 0.9)
        if end is not None:
            break
    assert end is not None and end.audio is not None
    assert end.seconds <= 1.05


def test_reset_drops_the_half_built_segment():
    seg = vad.Segmenter()
    for _ in range(15):
        assert seg.push(np.ones(N, np.float32), 0.9) is None
    seg.reset()  # she started speaking: forget everything mid-stream
    ends = [seg.push(np.zeros(N, np.float32), 0.0) for _ in range(100)]
    assert all(e is None for e in ends)


def test_mean_prob_is_the_voice_quality_reading():
    seg = vad.Segmenter()
    for _ in range(16):
        seg.push(np.ones(N, np.float32), 0.95)  # confident voice
    ends = [seg.push(np.zeros(N, np.float32), 0.05) for _ in range(30)]
    end = next(e for e in ends if e is not None)
    assert end.audio is not None
    assert abs(end.mean_prob - 0.95) < 1e-6  # voiced frames only, not diluted


def test_mean_prob_drops_for_marginal_frames():
    seg = vad.Segmenter()
    for _ in range(16):
        seg.push(np.ones(N, np.float32), 0.55)  # barely over the 0.5 threshold
    ends = [seg.push(np.zeros(N, np.float32), 0.0) for _ in range(30)]
    end = next(e for e in ends if e is not None)
    assert end.audio is not None and end.mean_prob < 0.6  # TV-noise shape


def test_prime_lets_pre_roll_cross_a_pause():
    """Frames banked while paused (tail era) seed the idle ring, so the next
    segment's pre-roll reaches back before the resume."""
    seg = vad.Segmenter()
    seg.prime([np.full(N, 7.0, np.float32) for _ in range(8)])  # tail audio
    ends = []
    for _ in range(20):
        ends.append(seg.push(np.ones(N, np.float32), 0.9))
    for _ in range(30):
        ends.append(seg.push(np.zeros(N, np.float32), 0.0))
    end = next(e for e in ends if e is not None)
    assert end.audio is not None
    # the clip opens with the primed tail audio (amplitude 7.0), not silence
    np.testing.assert_allclose(end.audio[: 5 * N], 7.0)


# ── EnergyVad fallback ────────────────────────────────────────
def test_energy_vad_learns_the_floor_and_gates_hysteretically():
    v = vad.EnergyVad()
    assert all(v(np.full(N, 0.001, np.float32)) == 0.0 for _ in range(60))
    assert all(v(np.full(N, 0.05, np.float32)) == 1.0 for _ in range(5))
    assert v(np.full(N, 0.002, np.float32)) == 1.0  # hysteresis: between close and open
    assert v(np.full(N, 0.001, np.float32)) == 0.0  # quiet again: gate closes


def test_energy_vad_end_to_end_segment():
    seg, v = vad.Segmenter(), vad.EnergyVad()
    ends = []
    for amp, n in ((0.001, 40), (0.05, 30), (0.001, 40)):
        for _ in range(n):
            ends.append(seg.push(np.full(N, amp, np.float32), v(np.full(N, amp, np.float32))))
    assert sum(e is not None for e in ends) == 1
    assert next(e for e in ends if e is not None).audio is not None


def test_make_vad_falls_back_to_energy(monkeypatch, capsys):
    def boom(*a, **k):
        raise ImportError("no onnxruntime here")

    monkeypatch.setattr(vad_mod, "SileroVad", boom)
    v, name = vad_mod.make_vad()
    assert isinstance(v, vad_mod.EnergyVad) and name == "energy"
    assert "energy fallback" in capsys.readouterr().out


@pytest.mark.skipif(importlib.util.find_spec("onnxruntime") is None, reason="onnxruntime missing")
def test_silero_loads_from_the_installed_package():
    faster_whisper = pytest.importorskip("faster_whisper")
    from faster_whisper.utils import get_assets_path

    model = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
    if not Path(model).exists():  # older wheel with a different bundled version
        candidates = sorted(Path(get_assets_path()).glob("silero_vad*.onnx"))
        if not candidates:
            pytest.skip("no bundled silero model in the installed wheel")
        model = str(candidates[0])
    v = vad.SileroVad(model_path=model)
    quiet = [v(np.zeros(N, np.float32)) for _ in range(10)]
    t = np.arange(N)
    loud = [v((0.3 * np.sin(2 * np.pi * 220 * t / SR)).astype(np.float32)) for _ in range(10)]
    assert all(0.0 <= p <= 1.0 for p in quiet + loud)
    assert max(loud) > max(quiet)  # speech-shaped energy reads higher than silence


# ── AlwaysListener (fake sounddevice, injected VAD) ──────────
def test_listener_delivers_a_wav_segment_with_stats():
    segs, stats_box, sd = [], [], FakeSD()
    listener = asr.AlwaysListener(collector(segs, stats_box), sd_module=sd,
                                  vad=vad.EnergyVad())
    listener.start()
    try:
        put_frames(sd, idle=20, loud=25, quiet=30)
        assert wait_for(lambda: segs), "no segment was delivered"
    finally:
        listener.close()
    import soundfile as sf

    info = sf.info(__import__("io").BytesIO(segs[0]))
    assert info.samplerate == SR and info.duration >= 1.0
    st = stats_box[0]  # the Silero evidence the noise filter weighs
    assert st["voiced_seconds"] >= 0.5 and st["mean_prob"] == 1.0  # EnergyVad: 1/0
    assert not listener.running
    assert sd.streams[-1].closed  # shutdown releases the device


def test_listener_pause_drops_frames():
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(collector(segs), sd_module=sd, vad=vad.EnergyVad())
    listener.start()
    try:
        put_frames(sd, idle=20)
        time.sleep(0.2)
        listener.pause()  # she is speaking: her own voice must not count
        put_frames(sd, loud=25, quiet=30)
        time.sleep(0.4)
        assert not segs, "paused VAD still delivered a segment"
        listener.resume()
        put_frames(sd, loud=25, quiet=30)
        assert wait_for(lambda: segs), "resume did not bring the ears back"
    finally:
        listener.close()


def test_listener_feeds_the_vad_while_paused():
    """Silero is recurrent: frames during her turn must still reach the model
    (state stays warm) even though segmentation ignores them."""
    calls, segs, sd = [], [], FakeSD()

    def probe(frame):
        calls.append(len(calls))
        return 0.0

    listener = asr.AlwaysListener(collector(segs), sd_module=sd, vad=probe)
    listener.start()
    try:
        listener.pause()
        put_frames(sd, loud=40)
        assert wait_for(lambda: len(calls) >= 20, timeout=3), \
            "paused listener stopped feeding the VAD (state would go stale)"
        time.sleep(0.3)
        assert not segs, "pause did not stop segmentation"
    finally:
        listener.close()


def test_listener_tail_prerolls_the_next_segment():
    """Frames between tail() (her voice over) and resume() may NOT be lost:
    the next segment back-dates into them, so syllables said during the echo
    tail are in the clip. Frames before tail() (her own voice) never are."""
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(collector(segs), sd_module=sd, vad=vad.EnergyVad())

    def emit(amp, n, pace=0.0):
        for _ in range(n):
            sd.frames.put(np.full(N, amp, np.float32))
            if pace:
                time.sleep(pace)  # wall-clock, so the reader banks them live

    listener.start()
    try:
        listener.pause()
        emit(0.5, 30, pace=0.03)  # her own voice while paused: never in a clip
        time.sleep(0.2)
        listener.tail()  # her audio really ended
        emit(0.03, 20, pace=0.03)  # owner answers during the echo tail (0.03 = marker)
        listener.resume()
        emit(0.05, 20)  # owner keeps talking after the resume
        emit(0.001, 40)  # silence closes the segment
        assert wait_for(lambda: segs, timeout=3), "no segment after tail+resume"
    finally:
        listener.close()
    import io

    import soundfile as sf
    audio, _rate = sf.read(io.BytesIO(segs[0]))
    frames = np.abs(audio[np.arange(0, len(audio), N)])
    assert not (frames > 0.2).any(), "her own voice leaked into the clip"
    # the clip back-dates across the resume: tail-era frames (amp 0.03) are
    # inside it, near the head. Without the seed they would be gone entirely.
    head = frames[: 15]  # first ~500 ms
    marks = int(((head > 0.02) & (head < 0.045)).sum())
    assert marks >= 2, f"only {marks} tail frames in the clip: pre-roll lost"
    voiced = int((frames > 0.02).sum()) * 32
    assert voiced >= 500, f"only {voiced} ms voiced: tail frames lost"


def test_listener_suspend_gives_the_mic_up_and_release_takes_it_back():
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(collector(segs), sd_module=sd, vad=vad.EnergyVad())
    listener.start()
    try:
        first = sd.streams[0]
        listener.suspend()  # Space held: the Recorder owns the device
        assert wait_for(lambda: first.stopped), "stream stayed open while suspended"
        listener.release()
        assert wait_for(lambda: len(sd.streams) > 1), "no fresh stream after release"
        put_frames(sd, idle=20, loud=25, quiet=30)
        assert wait_for(lambda: segs), "no segment after release"
    finally:
        listener.close()


def test_listener_retries_after_device_error():
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(collector(segs), sd_module=sd, vad=vad.EnergyVad(),
                                  retry_delay=0.05, retry_max=0.2)
    listener.start()
    try:
        sd.read_error = True
        put_frames(sd, idle=1)
        time.sleep(0.3)
        sd.read_error = False
        put_frames(sd, idle=20, loud=25, quiet=30)
        assert wait_for(lambda: segs, timeout=5), "listener died instead of retrying"
    finally:
        listener.close()


def test_listener_start_raises_on_a_dead_mic():
    listener = asr.AlwaysListener(lambda w: None, sd_module=FakeSD(fail_open=True),
                                  vad=vad.EnergyVad())
    with pytest.raises(OSError):
        listener.start()
    assert not listener.running


# ── evidence filter (the blocklist replacement) ──────────────
GOOD = {"segments": [{"no_speech_prob": 0.02, "avg_logprob": -0.2,
                      "compression_ratio": 1.3}]}
NONSPEECH = {"segments": [{"no_speech_prob": 0.9, "avg_logprob": -1.8,
                           "compression_ratio": 1.1}]}
RAMBLY = {"segments": [{"no_speech_prob": 0.1, "avg_logprob": -0.4,
                        "compression_ratio": 3.2}]}
VOICED = {"voiced_seconds": 0.8, "mean_prob": 0.95}   # a real voice per Silero
BLIP = {"voiced_seconds": 0.2, "mean_prob": 0.55}     # a marginal blip


def test_hallucination_reason_now_only_catches_nothing():
    """The phrase blocklist is gone: 'Thank you.' is a sentence, not a
    hallucination. Only empty / punctuation-only transcripts auto-drop."""
    short, long = wav_bytes(0.5, SR), wav_bytes(2.0, SR)
    assert asr.hallucination_reason("", short)
    assert asr.hallucination_reason("...!?..", short)
    assert asr.hallucination_reason("", short, noise_phrases=False)  # old signature
    for phrase in ("Thank you.", "Thanks for watching!", "Bye.", "you",
                   "subtitles by Person", "Please subscribe"):
        assert asr.hallucination_reason(phrase, short) is None, phrase
        assert asr.hallucination_reason(phrase, long) is None, phrase


def test_vad_noise_reason_with_whisper_metrics():
    # Whisper confident it heard words -> accept, including the phrases the
    # old blocklist used to eat.
    for phrase in ("Thank you.", "Thanks!", "Bye."):
        assert asr.vad_noise_reason(phrase, GOOD, VOICED) is None, phrase
    # Whisper itself says non-speech: every segment unsure -> drop.
    assert asr.vad_noise_reason("Thank you.", NONSPEECH, VOICED)
    # one rambly-repetition segment -> drop.
    assert asr.vad_noise_reason("Thank you thank you thank you", RAMBLY, VOICED)
    # mixed segments: only ONE looks like real speech -> accept (all() rule).
    mixed = {"segments": [NONSPEECH["segments"][0], GOOD["segments"][0]]}
    assert asr.vad_noise_reason("hello there", mixed, VOICED) is None
    # Silero never believed the voice even though Whisper wrote words -> drop.
    weak = {"voiced_seconds": 0.6, "mean_prob": 0.52}
    assert asr.vad_noise_reason("thank you", GOOD, weak)
    # empty stays empty.
    assert asr.vad_noise_reason("", GOOD, VOICED)
    assert asr.vad_noise_reason("...!", GOOD, VOICED)


def test_vad_noise_reason_fallback_without_metrics():
    """Old ASR server (verbose_json has no segments): accept only when Silero
    vouches hard: >= 0.4 s voiced at mean prob >= 0.7."""
    assert asr.vad_noise_reason("Thank you.", None, VOICED) is None
    assert asr.vad_noise_reason("Thank you.", {}, VOICED) is None
    assert asr.vad_noise_reason("Thank you.", {"segments": []}, VOICED) is None
    assert asr.vad_noise_reason("Thank you.", None, BLIP)  # too short/marginal
    assert asr.vad_noise_reason("Thank you.", None,
                                {"voiced_seconds": 0.8, "mean_prob": 0.65})
    assert asr.vad_noise_reason("Thank you.", None,
                                {"voiced_seconds": 0.35, "mean_prob": 0.95})
    assert asr.vad_noise_reason("", None, VOICED)  # empty still drops
    assert asr.vad_noise_reason("Thank you.", None)  # no stats at all -> drop


def test_segments_evidence_normalises_shapes():
    from types import SimpleNamespace

    objs = [SimpleNamespace(text="hi", no_speech_prob=0.01, avg_logprob=-0.3,
                            compression_ratio=1.1),
            SimpleNamespace(text="hi", no_speech_prob=None, avg_logprob=-0.3,
                            compression_ratio=1.1)]  # old server field missing
    ev = asr._segments_evidence(objs)
    assert ev == {"segments": [{"no_speech_prob": 0.01, "avg_logprob": -0.3,
                                "compression_ratio": 1.1}]}
    dicts = [{"no_speech_prob": 0.1, "avg_logprob": -0.5, "compression_ratio": 1.2}]
    assert asr._segments_evidence(dicts) == {"segments": dicts}


def test_transcribe_verbose_remote(monkeypatch):
    """The client asks for verbose_json and maps segments to evidence."""
    from types import SimpleNamespace

    calls = []

    class FakeAPI:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(
                text=" Thank you. ",
                segments=[SimpleNamespace(no_speech_prob=0.01, avg_logprob=-0.25,
                                          compression_ratio=1.4)])

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeAPI()))
    monkeypatch.setattr(asr, "_remote", client)
    cfg = {"asr": "remote", "remote_asr": {"model": "whisper-1"}}
    text, ev = asr.transcribe_verbose(b"RIFF", cfg)
    assert text == "Thank you."
    assert calls[0]["response_format"] == "verbose_json"
    assert ev == {"segments": [{"no_speech_prob": 0.01, "avg_logprob": -0.25,
                                "compression_ratio": 1.4}]}

    # an old server answering verbose_json WITHOUT segments -> evidence None
    class OldAPI:
        def create(self, **kw):
            return SimpleNamespace(text="Thank you.", segments=None)

    monkeypatch.setattr(asr, "_remote", SimpleNamespace(
        audio=SimpleNamespace(transcriptions=OldAPI())))
    text, ev = asr.transcribe_verbose(b"RIFF", cfg)
    assert (text, ev) == ("Thank you.", None)


def test_transcribe_verbose_local(monkeypatch):
    from types import SimpleNamespace

    segs = [SimpleNamespace(text=" Thank you.", no_speech_prob=0.02,
                            avg_logprob=-0.4, compression_ratio=1.2)]
    monkeypatch.setattr(asr, "_whisper", SimpleNamespace(
        transcribe=lambda *_a, **_k: (iter(segs), None)))
    text, ev = asr.transcribe_verbose(b"RIFF", {"asr": "local"})
    assert text == "Thank you."
    assert ev == {"segments": [{"no_speech_prob": 0.02, "avg_logprob": -0.4,
                                "compression_ratio": 1.2}]}


# ── appstate persistence ─────────────────────────────────────
def test_appstate_roundtrip_and_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    assert appstate.load_mode() is None
    assert appstate.save_mode("always") and appstate.load_mode() == "always"
    assert not appstate.save_mode("sideways")
    (tmp_path / "listen_mode.json").write_text("{not json", encoding="utf-8")
    assert appstate.load_mode() is None


# ── Session behaviour ────────────────────────────────────────
class SessionEnv:
    def __init__(self, cfg, url, listeners, heard_wavs):
        self.cfg, self.url, self.listeners, self.heard_wavs = cfg, url, listeners, heard_wavs

    def latest(self):
        return self.listeners[-1]


@pytest.fixture
def listen_env(monkeypatch, fake_hub, tmp_path):
    """Viewer + fake hub + fake AlwaysListener + fake transcription."""
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "state"))
    cfg = make_cfg(fake_hub.url)
    server = viewer.start(cfg)
    hub_client.connect(cfg)
    url = f"http://127.0.0.1:{port}"
    requests.get(f"{url}/next", timeout=5)

    env = {"fail": False}
    made, heard = [], []

    def factory(on_segment, cfg=None):
        listener = FakeListener(on_segment, cfg)
        if env["fail"]:
            raise OSError("no such device")
        made.append(listener)
        return listener

    monkeypatch.setattr(asr, "AlwaysListener", factory)
    monkeypatch.setattr(asr, "transcribe",
                        lambda wav, cfg=None: heard.append(wav) or "hello there")

    def fake_verbose(wav, cfg=None):
        heard.append(wav)
        return "hello there", GOOD

    monkeypatch.setattr(asr, "transcribe_verbose", fake_verbose)
    yield cfg, url, made, heard, env
    server.shutdown()
    server.server_close()


def drain(url, seconds=0.6, want=None):
    want = want or set()
    events, deadline = [], time.monotonic() + seconds
    seen = set()
    while time.monotonic() < deadline and not want <= seen:
        for e in requests.get(f"{url}/next", timeout=5).json()["events"]:
            events.append(e)
            seen.add(e["type"])
        time.sleep(0.05)
    return events


def make_session(listen_mode):
    cfg_mode = {"listen_mode": listen_mode}
    return cfg_mode


def test_always_mode_no_listening_pill(listen_env):
    cfg, url, made, heard, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    s = session.Session(cfg, source="desktop")
    assert s.listen_mode == "always" and len(made) == 1 and made[0].started == 1
    made[0].feed(wav_bytes(1.2, SR))  # the VAD "hears" an utterance
    events = drain(url, want={"say"}, seconds=6)
    types = [e["type"] for e in events]
    assert "heard" in types and "say" in types
    assert heard and heard[0]  # the wav went through the PTT transcription path
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "thinking" in statuses
    assert "listening" not in statuses, f"always mode must not show the pill: {statuses}"
    assert "hearing" not in statuses, f"VAD segments must not flash 'hmm?': {statuses}"
    s.close()


def test_push_to_talk_keeps_the_listening_pill(listen_env, monkeypatch):
    cfg, url, made, _, _ = listen_env
    fake_sd = FakeSD()
    monkeypatch.setattr(asr, "sd", fake_sd)  # Recorder reads module attr at call time
    s = session.Session(cfg, source="desktop")  # no listen_mode in cfg -> push_to_talk
    assert s.listen_mode == "push_to_talk" and made == []
    s.talk_start()
    events = drain(url, want={"status"}, seconds=2)
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "listening" in statuses
    s.talk_stop()
    s.close()


def test_space_in_always_mode_is_still_push_to_talk(listen_env, monkeypatch):
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    fake_sd = FakeSD()

    def factory(on_segment, cfg=None):  # real listener, fake hardware
        listener = RealAlwaysListener(on_segment, cfg=cfg, sd_module=fake_sd,
                                      vad=vad.EnergyVad())
        made.append(listener)
        return listener

    monkeypatch.setattr(asr, "AlwaysListener", factory)
    monkeypatch.setattr(asr, "sd", fake_sd)
    s = session.Session(cfg, source="desktop")
    listener = made[0]
    s.talk_start()  # explicit PTT on top of always mode
    events = drain(url, want={"status"}, seconds=2)
    assert "listening" in [e["state"] for e in events if e["type"] == "status"]
    first = fake_sd.streams[0]  # the VAD's stream, about to be handed over
    assert wait_for(lambda: first.stopped), "VAD kept the mic while Space was held"
    s.talk_stop()
    assert not listener._suspend.is_set()
    assert wait_for(lambda: len(fake_sd.streams) > 2), "VAD never took the mic back"
    s.close()


def test_vad_pauses_while_she_thinks_and_speaks(listen_env):
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    s = session.Session(cfg, source="desktop")
    listener = made[0]
    s.submit_text("hello there")
    assert wait_for(lambda: listener.pause_calls >= 1, timeout=5), "VAD kept listening mid-turn"
    assert wait_for(lambda: listener.resume_calls >= 1, timeout=8), "VAD never resumed"
    # no sound was played (tts: none): resume must be instant — no echo tail,
    # and tail() only ever belongs to a real playback.
    assert listener.calls == ["pause", "resume"], listener.calls
    drain(url)
    s.close()


def test_vad_thank_you_goes_through(listen_env, monkeypatch):
    """The owner's complaint #1, fixed: a real spoken 'Thank you.' with good
    Whisper evidence is a sentence, not a hallucination — she must answer it.
    (The old phrase blocklist ate exactly this clip.)"""
    cfg, url, made, heard, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe_verbose",
                        lambda wav, cfg=None: ("Thank you.", GOOD))
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR))
    events = drain(url, want={"say"}, seconds=6)
    heard_ev = [e for e in events if e["type"] == "heard"]
    assert heard_ev and heard_ev[0]["text"] == "Thank you.", events
    s.close()


def test_vad_noise_dropped_on_whisper_evidence(listen_env, monkeypatch):
    """Same words, but Whisper itself says non-speech: the clip is dropped on
    evidence, not on a phrase table."""
    cfg, url, made, heard, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe_verbose",
                        lambda wav, cfg=None: ("Thank you.", NONSPEECH))
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR))
    events = drain(url, want={"say"}, seconds=3.0)
    assert [e["type"] for e in events if e["type"] == "heard"] == []
    s.close()


def test_vad_fallback_without_metrics_uses_silero(listen_env, monkeypatch):
    """Old ASR server (no segments): a Silero-confirmed clip still gets
    through; a marginal blip does not."""
    cfg, url, made, heard, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe_verbose",
                        lambda wav, cfg=None: ("Thank you.", None))
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR), dict(BLIP))  # marginal: dropped
    events = drain(url, want={"say"}, seconds=2.0)
    assert [e["type"] for e in events if e["type"] == "heard"] == []
    made[0].feed(wav_bytes(0.5, SR), dict(VOICED))  # confident: accepted
    events = drain(url, want={"say"}, seconds=6)
    heard_ev = [e for e in events if e["type"] == "heard"]
    assert heard_ev and heard_ev[0]["text"] == "Thank you.", events
    s.close()


def test_vad_noise_phrase_emits_no_status_at_all(listen_env, monkeypatch):
    """The 'hmm?' fix: an ignored VAD segment must not even flash a pill —
    no hearing, no thinking, no error; at most the worker's idle reset, so
    the page can never be left on a stale state."""
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe_verbose",
                        lambda wav, cfg=None: ("Thank you.", NONSPEECH))
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR))  # Whisper-evidence noise: dropped
    events = drain(url, seconds=1.5)
    types = [e["type"] for e in events]
    assert "heard" not in types and "say" not in types and "error" not in types, types
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert set(statuses) <= {"idle"}, f"ignored VAD segment leaked status: {statuses}"
    s.close()


def test_ptt_thank_you_goes_through(listen_env, monkeypatch):
    """A deliberate push-to-talk 'Thank you.' is real speech: the noise-phrase
    blocklist belongs to VAD segments only."""
    cfg, url, made, heard, _ = listen_env
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg=None: "Thank you.")
    s = session.Session(cfg, source="desktop")  # push-to-talk
    s.submit_audio(wav_bytes(0.5, SR))  # the very clip the VAD path would drop
    events = drain(url, want={"say"}, seconds=6)
    heard_ev = [e for e in events if e["type"] == "heard"]
    assert heard_ev and heard_ev[0]["text"] == "Thank you.", events
    assert any(e["type"] == "say" for e in events), events  # she answered it
    s.close()


def test_ptt_turn_still_shows_the_hearing_pill(listen_env):
    cfg, url, made, _, _ = listen_env
    s = session.Session(cfg, source="desktop")
    s.submit_audio(wav_bytes(1.2, SR))
    events = drain(url, want={"say"}, seconds=6)
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "hearing" in statuses and "thinking" in statuses, statuses
    s.close()


def test_vad_real_text_no_hearing_but_heard_and_thinking(listen_env):
    """A VAD segment that passes the filter: no 'hmm?' beforehand, but the
    normal heard -> thinking -> say flow still plays."""
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(1.2, SR))
    events = drain(url, want={"say"}, seconds=6)
    statuses = [e["state"] for e in events if e["type"] == "status"]
    assert "hearing" not in statuses, statuses
    assert "thinking" in statuses, statuses
    assert any(e["type"] == "heard" for e in events), events
    s.close()


def test_heard_nothing_hint_is_ptt_only(listen_env, monkeypatch):
    """Empty transcripts drop in BOTH sources, but only the deliberate
    push-to-talk clip earns '(heard nothing, try again)' — even in always
    mode, where Space-held PTT rides the same submit_audio path."""
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg=None: "")
    monkeypatch.setattr(asr, "transcribe_verbose", lambda wav, cfg=None: ("", None))
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR))  # VAD silence: dropped, no hint
    s.submit_audio(wav_bytes(0.5, SR))  # PTT silence: asks again
    events = drain(url, want={"error"}, seconds=6)
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1 and "try again" in errors[0]["text"], events
    assert [e for e in events if e["type"] == "heard"] == []
    s.close()


def test_toggle_persists_and_restores(listen_env):
    cfg, url, made, _, _ = listen_env
    s = session.Session(cfg, source="desktop")  # push_to_talk (no state file, no seed)
    assert appstate.load_mode() is None
    s.toggle_listen_mode()
    assert s.listen_mode == "always" and appstate.load_mode() == "always"
    assert len(made) == 1 and made[0].started == 1
    s.toggle_listen_mode()
    assert s.listen_mode == "push_to_talk" and appstate.load_mode() == "push_to_talk"
    assert made[0].closed_count == 1
    s.close()
    # the next start honours the persisted choice, not the yaml seed
    appstate.save_mode("always")
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="push_to_talk")
    s2 = session.Session(cfg, source="desktop")
    assert s2.listen_mode == "always" and made[-1].started == 1
    s2.close()


def test_config_seed_sets_the_initial_default(listen_env):
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    s = session.Session(cfg, source="desktop")
    assert s.listen_mode == "always" and made[0].started == 1
    assert appstate.load_mode() is None  # config is a seed only: nothing persisted yet
    s.close()


def test_mic_failure_falls_back_to_push_to_talk(listen_env):
    cfg, url, made, _, env = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    env["fail"] = True  # AlwaysListener.start raises: dead mic
    s = session.Session(cfg, source="desktop")
    assert s.listen_mode == "push_to_talk" and s._vad is None
    assert appstate.load_mode() is None  # a failed switch is not remembered
    s.toggle_listen_mode()  # explicit toggle onto the broken mic stays PTT
    assert s.listen_mode == "push_to_talk"
    env["fail"] = False
    s.toggle_listen_mode()
    assert s.listen_mode == "always" and appstate.load_mode() == "always"
    s.close()


def test_tunables_reach_the_listener(listen_env):
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always",
                          always_listen={"threshold": 0.6, "max_seconds": 5})
    s = session.Session(cfg, source="desktop")
    assert made[0].cfg == {"threshold": 0.6, "max_seconds": 5}
    s.close()


# ── wiring: tray/control/shortcut/cli/page ────────────────────
ROOT = Path(__file__).resolve().parent.parent


def test_mode_switch_is_wired_everywhere():
    desktop = (ROOT / "desktop.py").read_text(encoding="utf-8")
    assert 'elif cmd == "listen-mode"' in desktop
    assert "Always listen" in desktop and "listen-ui:" in desktop
    assert '"listen_mode": session.listen_mode' in desktop  # riko-ctl status shows it
    ctl = (ROOT / "bin" / "riko-ctl").read_text(encoding="utf-8")
    assert '"listen-mode"' in ctl
    page = (ROOT / "client" / "index.html").read_text(encoding="utf-8")
    assert "action === 'listen_mode'" in page
    import riko.shortcuts as sc

    assert sc.DEFAULTS["listen_mode"] == ("L",)
    yaml_text = (ROOT / "character_config.yaml").read_text(encoding="utf-8")
    assert "listen_mode:" in yaml_text and "always_listen:" in yaml_text


def test_close_releases_the_listener(listen_env):
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    s = session.Session(cfg, source="desktop")
    s.close()
    assert made[0].closed_count == 1 and s._vad is None
