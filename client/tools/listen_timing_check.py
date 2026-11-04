#!/usr/bin/env python3
"""Headless timing QA for always-listen: her voice end -> the VAD armed again.

Owner complaint: after Riko finishes speaking she is deaf for a second or two,
so the start of the reply to her is lost. This check MEASURES that gap with
real components on real clocks:

  real riko.viewer + the real client/index.html in headless Chromium
  + a real riko.session.Session in always-listen mode
  + a real riko.asr.AlwaysListener on a fake mic (silence frames) and a probe
    VAD that timestamps every frame it is fed
  + a fake hub (tests/conftest) and a fake TTS that hands out a 1.5 s tone wav

Sequence: the page loads, Session gets a typed turn, she "speaks" the tone on
the page, the page POSTs /done when the audio's 'ended' fires, and the worker
eventually resumes the VAD. Every marker is time.monotonic() in THIS process;
the page-side end is stamped by the arrival of its /done POST (loopback fetch
latency is a few ms).

Printed gaps (ms):
  done->play_return   the page's report reaching viewer.play
  done->vad_resume    worker bookkeeping + echo tail before resume()
  done->first_frame   first mic frame actually fed to the segmenter after that
  vad_calls_while_speaking  VAD frames fed while her audio ran (0 = Silero
                      state goes stale across the pause)

Exit code 0 = done->vad_resume within the --target budget (default 300 ms).
Never opens the owner's mic/speakers; Chromium gets its own temp profile.

Usage:
    uv run python client/tools/listen_timing_check.py [--target-ms 300]
"""
import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests")]
TMP = os.environ.get("TMPDIR", str(Path.home() / ".hermes/cache/scratch"))

import queue  # noqa: E402

SR = 16000
AUDIO_SECONDS = 1.5
FAKE_HUB_KEY = "test-hub-key"


# ── fake mic (same shape as tests/test_listen_mode) ──────────
class FakeStream:
    def __init__(self, sd):
        self.sd = sd

    def start(self):
        pass

    def read(self, n):
        try:
            item = self.sd.frames.get(timeout=0.02)
        except queue.Empty:
            item = None
        return (np.zeros(n, np.float32) if item is None else item), False

    def stop(self):
        pass

    def close(self):
        pass


class FakeSD:
    def __init__(self):
        self.streams = []
        self.frames = queue.Queue()

    def InputStream(self, **kwargs):  # noqa: N802 (sounddevice API)
        s = FakeStream(self)
        self.streams.append(s)
        return s


class ProbeVad:
    """Timestamps every frame it is fed and whether the listener was paused."""

    def __init__(self):
        self.listener = None
        self.calls = []  # (monotonic, paused)

    def __call__(self, frame):
        paused = bool(self.listener and self.listener._pause.is_set())
        self.calls.append((time.monotonic(), paused))
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-ms", type=float, default=300.0)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    tmp_state = tempfile.mkdtemp(prefix="riko-timing-", dir=TMP)
    os.environ["RIKO_APP_STATE_DIR"] = os.path.join(tmp_state, "appstate")
    os.environ["RIKO_ROAM_STATE"] = os.path.join(tmp_state, "roam_x.json")
    os.environ["RIKO_CONFIG_DIR"] = os.path.join(tmp_state, "config")
    os.environ["RIKO_AVATAR_CACHE"] = os.path.join(tmp_state, "avatars")
    os.environ["FAKE_HUB_KEY"] = FAKE_HUB_KEY

    import conftest  # tests/conftest.py: FakeHub, make_cfg, free_port
    # The page must load a REAL VRM or it reload-loops on the fake bytes.
    vrm_bytes = (REPO / "character_files" / "sample_avatar.vrm").read_bytes()
    conftest.avatar_bytes = lambda pid, ver: vrm_bytes
    hub = conftest.FakeHub()

    from riko import asr, hub_client, session as session_mod, tts, viewer

    port = conftest.free_port()
    os.environ["RIKO_PORT"] = str(port)
    cfg = conftest.make_cfg(hub.url)
    cfg["desktop"] = dict(cfg.get("desktop") or {}, listen_mode="always")

    markers = {}

    def stamp(name):
        markers.setdefault(name, time.monotonic())

    # fake TTS: a plain tone written where the page can fetch it
    def fake_speak(text, cfg_, voice=None, speech=None):
        tts.VOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        t = np.arange(int(SR * AUDIO_SECONDS)) / SR
        tone = (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        sf.write(tts.VOICE_FILE, tone, SR)
        return tts.VOICE_FILE

    tts.speak = fake_speak

    # /done arrival == the page's audio 'ended' (plus loopback fetch latency)
    class StampedEvent(threading.Event):
        def set(self):
            stamp("audio_end_done_post")
            super().set()

    viewer._finished = StampedEvent()
    real_play = viewer.play

    def timed_play(*a, **k):
        try:
            return real_play(*a, **k)
        finally:
            stamp("play_return")

    viewer.play = timed_play

    # real AlwaysListener over fake hardware, pause/resume/tail timestamped
    fake_sd = FakeSD()
    probe = ProbeVad()
    made = []

    real_listener_cls = asr.AlwaysListener

    def factory(on_segment, cfg=None):
        listener = real_listener_cls(on_segment, cfg=cfg, sd_module=fake_sd, vad=probe)
        for name in ("pause", "resume", "tail"):
            orig = getattr(listener, name, None)
            if orig is None:
                continue  # tail(): pre-fix builds have no tail marker
            def wrap(orig=orig, name=name):
                stamp(f"vad_{name}")
                return orig()
            setattr(listener, name, wrap)
        probe.listener = listener
        made.append(listener)
        return listener

    asr.AlwaysListener = factory
    asr.sd = fake_sd  # Recorder must never touch a real device either

    server = viewer.start(cfg)
    hub_client.connect(cfg)
    s = session_mod.Session(cfg, source="desktop")

    proc = None
    rc = 1
    try:
        if not made:
            raise RuntimeError("Session did not build an AlwaysListener")
        profile = os.path.join(tmp_state, "chrome-profile")
        url = f"http://127.0.0.1:{port}/?desktop=1"
        cmd = ["chromium", "--headless=new", f"--user-data-dir={profile}", "--no-sandbox",
               "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
               "--autoplay-policy=no-user-gesture-required", "--window-size=500,700",
               "--disable-dev-shm-usage", "--hide-scrollbars", "--allow-file-access", url]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        deadline = time.monotonic() + args.timeout
        while not viewer.page_open() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not viewer.page_open():
            raise RuntimeError("the avatar page never polled /next (chromium failed?)")
        stamp("submit")
        s.submit_text("timing check turn")
        deadline = time.monotonic() + args.timeout
        while "vad_resume" not in markers and time.monotonic() < deadline:
            time.sleep(0.02)
        if "vad_resume" not in markers:
            raise RuntimeError(f"VAD never resumed; markers={sorted(markers)}")
        # first mic frame fed AFTER the resume (the ear is actually armed)
        deadline = time.monotonic() + 2.0
        first_after = None
        while first_after is None and time.monotonic() < deadline:
            first_after = next((t for t, _ in probe.calls if t >= markers["vad_resume"]), None)
            if first_after is None:
                time.sleep(0.02)
        if first_after is not None:
            stamp("first_frame_after_resume")
            markers["first_frame_after_resume"] = first_after
        # VAD frames fed while her audio was running (Silero warmth)
        if "audio_end_done_post" in markers:
            lo = markers["audio_end_done_post"] - AUDIO_SECONDS
            hi = markers["audio_end_done_post"]
            n_speaking = sum(1 for t, paused in probe.calls if lo <= t <= hi and paused)
            markers["vad_calls_while_speaking"] = n_speaking

        ms = lambda a, b: (markers[b] - markers[a]) * 1000.0  # noqa: E731
        print(f"[timing] audio={AUDIO_SECONDS}s  viewer port={port}")
        print(f"[timing] submit->speaking_report : {ms('submit','audio_end_done_post'):8.1f} ms")
        print(f"[timing] audio_end->play_return  : {ms('audio_end_done_post','play_return'):8.1f} ms")
        print(f"[timing] audio_end->vad_resume   : {ms('audio_end_done_post','vad_resume'):8.1f} ms")
        if "first_frame_after_resume" in markers:
            print(f"[timing] audio_end->first_frame  : "
                  f"{ms('audio_end_done_post','first_frame_after_resume'):8.1f} ms")
        print(f"[timing] vad frames fed while her audio ran (paused): "
              f"{markers.get('vad_calls_while_speaking', 'n/a')}")
        gap = ms("audio_end_done_post", "vad_resume")
        ok = gap <= args.target_ms
        print(f"[timing] armed gap {gap:.0f} ms  target {args.target_ms:.0f} ms  "
              f"-> {'PASS' if ok else 'FAIL'}")
        rc = 0 if ok else 1
    finally:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)
        s.close()
        server.shutdown()
        server.server_close()
        hub.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
