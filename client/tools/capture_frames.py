#!/usr/bin/env python3
"""Headless visual QA for Riko's avatar page (no window on your desktop).

Serves client/ with stubbed /next, /settings, /animations.json and
/avatar.vrm (the real character_files/sample_avatar.vrm), opens it in a
headless Chromium with its OWN temp profile (never the owner's), and lets
the page's ?capture=<secs> hook POST PNGs to /capture and pose metrics
(window.rikoMeasure) to /measure.

Usage:
    python client/tools/capture_frames.py <scenario> <outdir> <capture-secs>
                                          [--force spec] [--runtime SECS]

Scenarios: idle | thinking | say | think | wave
  think/wave drive the page with ?force= (the director is silenced and the
  named gesture plays at a fixed time), so captures land at exact pose times.
--force accepts the page's spec directly, e.g. --force "wave@0.5,think@4".

The metrics JSON is printed per capture (distances in metres, computed in
the page against the real rig; chin point defined in client/index.html).
Acceptance reference (owner request 2026-11):
  think: handToChin < 0.07, leftHandToRightElbow < 0.12,
         hand/elbow not behind the chest plane (z >= chest.z, slack 0.05),
         held at 1 s, 5 s and 20 s into the loop.
  wave:  handAboveShoulderY > 0.15 at peak, lateral hand x swing > 0.06
         peak-to-peak across the oscillation, hand >= head top - 5 cm.
"""
import argparse
import base64
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
VRM = str(REPO / "character_files" / "sample_avatar.vrm")
TMP = os.environ.get("TMPDIR", str(Path.home() / ".hermes/cache/scratch"))

# scenario -> (force spec for the page, queued /next events)
SCENARIOS = {
    "idle": (None, []),  # calm by default
    "thinking": (None, [(6.0, {"type": "status", "state": "thinking"})]),
    "say": (None, [
        (6.0, {"type": "status", "state": "speaking"}),
        (6.5, {"type": "say", "text": "Hi senpai! I fixed my neck :)", "emotion": "happy",
               "seconds": 3}),
        (10.0, {"type": "status", "state": "idle"}),
    ]),
    "think": ("think@0", []),
    "wave": ("wave@0", []),
}


class Handler(SimpleHTTPRequestHandler):
    t0 = time.monotonic()
    queue = []
    outdir = "."
    scenario = "idle"
    shots = 0

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/settings":
            return self._json({})
        if path == "/animations.json":
            return self._json({"animations": []})
        if path == "/avatar.vrm":
            with open(VRM, "rb") as f:
                return self._send(f.read(), "model/gltf-binary")
        if path == "/next":
            return self._json({"audio": None, "events": self._due()})
        return super().do_GET()

    def do_POST(self):
        m = re.match(r"/(capture|measure)\?t=([\d.]+)", self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if not m:
            return self._json({"ok": True})
        kind, t = m.group(1), float(m.group(2))
        stem = f"{self.scenario}-t{t:05.1f}s-{self.shots}"
        if kind == "capture":
            data = body.split(",", 1)[1] if "," in body[:64] else body
            name = stem + ".png"
            with open(os.path.join(self.outdir, name), "wb") as f:
                f.write(base64.b64decode(data))
            print(f"[capture] wrote {name}", flush=True)
        else:  # /measure: keep the metrics JSON and echo the interesting bits
            name = stem + ".json"
            with open(os.path.join(self.outdir, name), "w") as f:
                f.write(body)
            try:
                met = json.loads(body).get("metrics") or {}
            except json.JSONDecodeError:
                met = {}
            pretty = "  ".join(f"{k}={v:.3f}" if isinstance(v, (int, float)) else f"{k}={v}"
                               for k, v in met.items())
            print(f"[measure] t={t:.1f}s {pretty}", flush=True)
            self.shots += 1  # one shot per capture+measure pair
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("outdir")
    ap.add_argument("secs", help="comma-separated capture times after wake, e.g. 1,5,20")
    ap.add_argument("--force", default=None, help="page ?force= spec (overrides scenario)")
    ap.add_argument("--runtime", type=float, default=None,
                    help="browser lifetime (default: last capture + 8 s)")
    args = ap.parse_args()

    force, events = SCENARIOS[args.scenario]
    if args.force:
        force = args.force
    os.makedirs(args.outdir, exist_ok=True)
    Handler.scenario = args.scenario
    Handler.outdir = args.outdir
    Handler.queue = list(events)
    Handler.t0 = time.monotonic()  # server clock vs page clock differ a bit; fine
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # fresh port per run: strays from an old run hit nothing
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=CLIENT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    profile = os.path.join(TMP, "riko-chrome-profile")
    subprocess.run(["rm", "-rf", profile], check=False)  # temp profile, never the owner's
    url = f"http://127.0.0.1:{port}/?capture={args.secs}"
    if force:
        url += f"&force={force}"
    maxsec = max(float(s) for s in args.secs.split(","))
    dur = args.runtime if args.runtime else maxsec + 8
    cmd = [
        "chromium", "--headless=new", f"--user-data-dir={profile}", "--no-sandbox",
        "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
        "--autoplay-policy=no-user-gesture-required", "--window-size=620,780",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        "--allow-file-access", url,
    ]
    print(f"[harness] chromium for {dur}s -> {args.outdir}", flush=True)
    # own process group: chromium forks children that survive a plain proc.kill()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        time.sleep(dur)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    time.sleep(1.0)  # let in-flight /capture POSTs land
    srv.shutdown()
    shots = sorted(p for p in os.listdir(args.outdir) if p.endswith(".png"))
    print(f"[harness] scenario={args.scenario} shots={len(shots)}")
    for s in shots:
        print("  ", os.path.join(args.outdir, s))
    return 0 if shots else 1


if __name__ == "__main__":
    sys.exit(main())
