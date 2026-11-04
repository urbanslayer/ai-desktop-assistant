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
    """Same surface as asr.AlwaysListener: counters instead of threads/mic."""

    def __init__(self, on_segment, cfg=None):
        self.on_segment = on_segment
        self.cfg = cfg or {}
        self.started = 0
        self.closed_count = 0
        self.pause_calls = self.resume_calls = 0
        self.suspended = self.released = 0

    def start(self):
        self.started += 1

    def close(self):
        self.closed_count += 1

    def pause(self):
        self.pause_calls += 1

    def resume(self):
        self.resume_calls += 1

    def suspend(self):
        self.suspended += 1

    def release(self):
        self.released += 1

    def feed(self, wav):  # test hook: pretend the VAD finished an utterance
        self.on_segment(wav)


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
def test_listener_delivers_a_wav_segment():
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(segs.append, sd_module=sd, vad=vad.EnergyVad())
    listener.start()
    try:
        put_frames(sd, idle=20, loud=25, quiet=30)
        assert wait_for(lambda: segs), "no segment was delivered"
    finally:
        listener.close()
    import soundfile as sf

    info = sf.info(__import__("io").BytesIO(segs[0]))
    assert info.samplerate == SR and info.duration >= 1.0
    assert not listener.running
    assert sd.streams[-1].closed  # shutdown releases the device


def test_listener_pause_drops_frames():
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(segs.append, sd_module=sd, vad=vad.EnergyVad())
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


def test_listener_suspend_gives_the_mic_up_and_release_takes_it_back():
    segs, sd = [], FakeSD()
    listener = asr.AlwaysListener(segs.append, sd_module=sd, vad=vad.EnergyVad())
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
    listener = asr.AlwaysListener(segs.append, sd_module=sd, vad=vad.EnergyVad(),
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


# ── hallucination filter ─────────────────────────────────────
def test_hallucination_filter():
    # NB: conftest.wav_bytes(seconds) actually yields ~2x that duration
    short, long = wav_bytes(0.5, SR), wav_bytes(2.0, SR)
    assert asr.hallucination_reason("", short)
    assert asr.hallucination_reason("...!?..", short)
    for phrase in ("Thank you.", "Thanks for watching!", "you", ".", "Bye.",
                   "subtitles by Person", "Please subscribe"):
        assert asr.hallucination_reason(phrase, short), phrase
    assert asr.hallucination_reason("Thank you.", long) is None  # a real "thank you" can be long
    assert asr.hallucination_reason("hello there senpai", short) is None
    # push-to-talk (noise_phrases=False): the deliberate-clip rule — a spoken
    # "Thank you." passes, but silence/punctuation-only still drop.
    assert asr.hallucination_reason("Thank you.", short, noise_phrases=False) is None
    assert asr.hallucination_reason("subtitles by Person", short, noise_phrases=False) is None
    assert asr.hallucination_reason("", short, noise_phrases=False)
    assert asr.hallucination_reason("...!?..", short, noise_phrases=False)


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
    drain(url)
    s.close()


def test_short_noise_reply_is_not_heard_as_words(listen_env, monkeypatch):
    cfg, url, made, heard, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg=None: "Thank you.")
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR))  # ~1 s clip: classic short-clip hallucination
    # if the filter failed, a say would arrive; wait for it, then assert silence
    events = drain(url, want={"say"}, seconds=3.0)
    assert [e["type"] for e in events if e["type"] == "heard"] == []
    s.close()


def test_vad_noise_phrase_emits_no_status_at_all(listen_env, monkeypatch):
    """The 'hmm?' fix: an ignored VAD segment must not even flash a pill —
    no hearing, no thinking, no error; at most the worker's idle reset, so
    the page can never be left on a stale state."""
    cfg, url, made, _, _ = listen_env
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg=None: "Thank you.")
    s = session.Session(cfg, source="desktop")
    made[0].feed(wav_bytes(0.5, SR))  # ~1 s: the blocklist would normally eat this
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
