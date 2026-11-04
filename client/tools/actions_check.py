#!/usr/bin/env python3
"""Headless timing QA for reply body actions (docs/BODY.md §5).

Like capture_frames.py: serves client/ with stubbed /next, /settings,
/animations.json and /avatar.vrm, opens the page in a headless Chromium
with its OWN temp profile (never the owner's), this time with
?actionstiming=<s>&desktop=1. The page records every gesture start/stop
plus a marker when a say event carrying actions arrives into
window.__actionTimeline and POSTs it to /timeline after <s> seconds.

The scenario feeds, against the server clock:
  10 s   status thinking              (a think LOOP runs, so the running-loop
                                       path is exercised before the reply)
  32.4 s status speaking              (stops the loop; speaking state active)
  33.0 s say seconds=4 emotion=neutral
         actions [{wave, at:0}, {think, at:0.6}]
  (say is late on purpose: the page's own start-up wave puts wave on a 25 s
   cooldown measured on the PAGE clock, and here nobody asked for it, so it
   must be past that; navigation lag cancels out because every assertion is
   made against the say marker inside the page clock.)

Assertions (against the say marker INSIDE the page clock, so browser launch
lag cancels out):
  wave starts ~0 s after the say  (|dt| <= 0.6 s: 150 ms poll + one frame)
  wave stops before think starts  (one-shots are never interrupted)
  think starts ~2.4 s after the say (= 0.6 * 4 s speech, |dt| <= 0.8 s)

Exit code 0 = timings good. Prints the raw timeline either way.

Usage:
    python client/tools/actions_check.py [--runtime SECS] [--vrm PATH]
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLIENT = str(REPO / "client")
DEFAULT_VRM = str(REPO / "character_files" / "sample_avatar.vrm")
TMP = os.environ.get("TMPDIR", str(Path.home() / ".hermes/cache/scratch"))

SAY_AT = 33.0
SAY_SECONDS = 4.0
ACTIONS = [{"name": "wave", "at": 0}, {"name": "think", "at": 0.6}]
EVENTS = [
    (10.0, {"type": "status", "state": "thinking"}),
    (32.4, {"type": "status", "state": "speaking"}),
    (SAY_AT, {"type": "say", "text": "Sure thing, watch this.",
              "emotion": "neutral", "seconds": SAY_SECONDS, "actions": ACTIONS}),
]


class Handler(SimpleHTTPRequestHandler):
    t0 = time.monotonic()
    queue = []
    timeline = None
    vrm_path = DEFAULT_VRM

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/settings":
            return self._json({})
        if path == "/animations.json":
            return self._json({"animations": []})
        if path == "/avatar.vrm":
            with open(self.vrm_path, "rb") as f:
                return self._send(f.read(), "model/gltf-binary")
        if path == "/next":
            return self._json({"audio": None, "events": self._due()})
        return super().do_GET()

    def do_POST(self):
        m = re.match(r"/timeline", self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if m:
            try:
                self.timeline = json.loads(body)
            except json.JSONDecodeError:
                self.timeline = []
            print(f"[timeline] received {len(self.timeline)} entries", flush=True)
            return self._json({"ok": True})
        # /control/body-capabilities and friends: accept and ignore
        return self._json({"ok": True})

    def _due(self):
        now = time.monotonic() - self.t0
        due = [ev for (at, ev) in self.queue if at <= now]
        self.queue = [(at, ev) for (at, ev) in self.queue if at > now]
        return due

    def _json(self, obj):
        return self._send(json.dumps(obj).encode(), "application/json")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(timeline):
    """Assert the schedule against the say marker; returns (ok, report lines)."""
    say = next((e for e in timeline if e.get("type") == "say"), None)
    if say is None:
        return False, ["FAIL: no say marker in the timeline"]
    t0 = say["t"]
    starts = [e for e in timeline if e.get("type") == "gesture"]
    stops = [e for e in timeline if e.get("type") == "gesture_stop"]
    wave = next((e for e in starts if e["name"] == "wave" and e["t"] >= t0 - 0.05), None)
    think = next((e for e in starts if e["name"] == "think" and e["t"] >= t0 - 0.05), None)
    report = []
    ok = True
    if wave is None:
        ok = False
        report.append("FAIL: wave never started after the say")
    else:
        dt = wave["t"] - t0
        good = abs(dt) <= 0.6
        ok = ok and good
        report.append(f"{'ok  ' if good else 'FAIL'}: wave starts {dt:+.2f}s after say "
                      f"(want ~0.0, tol 0.6)")
    wave_stop = next((e for e in stops if e["name"] == "wave"
                      and wave and e["t"] >= wave["t"]), None)
    if think is None:
        ok = False
        report.append("FAIL: think never started after the say")
    else:
        dt = think["t"] - t0
        want = 0.6 * SAY_SECONDS  # at=0.6 of a 4 s reply -> 2.4 s
        good = abs(dt - want) <= 0.8
        ok = ok and good
        report.append(f"{'ok  ' if good else 'FAIL'}: think starts {dt:+.2f}s after say "
                      f"(want ~{want:.1f}, tol 0.8)")
        if wave_stop is None or wave_stop["t"] > think["t"]:
            ok = False
            report.append("FAIL: the wave one-shot was not stopped before think started")
        else:
            report.append(f"ok  : wave stopped {wave_stop['t'] - t0:+.2f}s, "
                          f"before think at {dt:+.2f}s")
    return ok, report


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runtime", type=float, default=38.0,
                    help="browser lifetime (default 38 s)")
    ap.add_argument("--vrm", default=DEFAULT_VRM)
    args = ap.parse_args()

    Handler.queue = list(EVENTS)
    Handler.vrm_path = args.vrm
    Handler.t0 = time.monotonic()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=CLIENT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    timing = int(args.runtime - 4)
    profile = os.path.join(TMP, "riko-chrome-profile-actions")
    subprocess.run(["rm", "-rf", profile], check=False)  # temp profile, never the owner's
    url = f"http://127.0.0.1:{port}/?actionstiming={timing}&desktop=1"
    cmd = [
        "chromium", "--headless=new", f"--user-data-dir={profile}", "--no-sandbox",
        "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
        "--autoplay-policy=no-user-gesture-required", "--window-size=420,680",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        "--allow-file-access", url,
    ]
    print(f"[harness] chromium {args.runtime}s, say at ~{SAY_AT}s "
          f"(wave@0, think@{0.6 * SAY_SECONDS}s of {SAY_SECONDS}s speech)", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        time.sleep(args.runtime)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    time.sleep(1.0)  # let the /timeline POST land
    srv.shutdown()

    timeline = Handler.timeline
    if timeline is None:
        print("FAIL: the page never POSTed /timeline")
        return 1
    for e in timeline:
        print("   ", json.dumps(e))
    ok, report = check(timeline)
    print("\n".join(report))
    print("ACTIONS CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
