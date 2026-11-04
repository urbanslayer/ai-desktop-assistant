#!/usr/bin/env python3
"""Headless regression check for the W shortcut (owner bug: "W doesn't wave").

Serves client/ with stubbed /next, /settings, /animations.json and
/avatar.vrm (like capture_frames.py), opens the page as
/?desktop=1&wavetest=1 in a headless Chromium with its OWN temp profile, and
lets the page's ?wavetest hook fire real KeyboardEvent('keydown', {key:'w'})
at fixed seconds after she woke up:

    after-startup-wave     t=3.5 s — the startup wave (2.4 s) has just ended,
                           inside the 25 s greeting cooldown: W must wave anyway
    already-waving-again   t=4.2 s — a wave should already be running: W must
                           not restack (same gesture, same end time)
    during-idle-gesture    t=8.2 s — an idle one-shot is playing (the hook
                           armed director.nextIdleAt at 7.5 s): W must cut in
                           front of it right away, not queue behind it

The hook POSTs the director's state to /wavetest; this script asserts:
  * the keydown handler ran every time (it is not a key-matching problem),
  * W always ends with a wave PLAYING (not pending, not nothing),
  * a press while waving changes nothing.

Exit 0 = pass, 1 = fail. No window on your desktop; no hub needed.

Usage: python client/tools/wave_check.py [--vrm PATH] [--secs N]
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

IDLE_GESTURES = {"stretch", "look_around", "fidget", "hair_touch"}


class Handler(SimpleHTTPRequestHandler):
    vrm_path = DEFAULT_VRM
    report = None  # the page's /wavetest POST body

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/settings":
            # exactly the shape the page keys its shortcuts off (riko/shortcuts.py
            # DEFAULTS for wave; the real one comes from viewer._desktop_settings)
            return self._json({"shortcuts": {"wave": ["W"], "hide": ["H"]}})
        if path == "/animations.json":
            return self._json({"animations": []})
        if path == "/avatar.vrm":
            with open(self.vrm_path, "rb") as f:
                return self._send(f.read(), "model/gltf-binary")
        if path == "/next":
            return self._json({"audio": None, "events": []})
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if re.match(r"/wavetest\b", self.path):
            Handler.report = json.loads(body)
            print("[wavetest] report received", flush=True)
        return self._json({"ok": True})

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


def check(report):
    """Assert the three presses; returns a list of failure strings."""
    fails = []
    by_label = {e["label"]: e for e in report}
    for label in ("after-startup-wave", "already-waving-again", "during-idle-gesture"):
        if label not in by_label:
            fails.append(f"no report entry for {label}")
    if fails:
        return fails

    e1 = by_label["after-startup-wave"]
    if not e1["handlerRan"]:
        fails.append("the keydown handler never reached requestWave (key path is broken)")
    if (e1.get("after") or {}).get("name") != "wave":
        fails.append(f"W at t={e1['t']}s (cooldown, body free) played {e1.get('after')!r}, "
                     "expected the wave to PLAY (cooldown must not swallow it)")

    e2 = by_label["already-waving-again"]
    first_end = (e1.get("after") or {}).get("endsAt")
    if first_end is not None:
        if (e2.get("after") or {}).get("endsAt") != first_end:
            fails.append("W while already waving restacked/restarted the wave")
        if e2["pendingNow"] == "wave":
            fails.append("W while already waving queued a second pending wave")

    e3 = by_label["during-idle-gesture"]
    if not e3["handlerRan"]:
        fails.append("the keydown handler never reached requestWave (2nd press)")
    if (e3.get("before") or {}).get("name") not in IDLE_GESTURES:
        fails.append(f"setup problem: no idle gesture was playing at t={e3['t']}s "
                     f"(before={e3.get('before')!r})")
    if (e3.get("after") or {}).get("name") != "wave":
        fails.append(f"W at t={e3['t']}s during {e3.get('before')!r} left "
                     f"gesture={e3.get('after')!r} pending={e3.get('pending')!r}, "
                     "expected the wave to PREEMPT the idle gesture right away")

    print("\n".join(f"  {json.dumps(e)}" for e in report))
    return fails


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vrm", default=DEFAULT_VRM)
    ap.add_argument("--secs", type=float, default=14, help="browser lifetime")
    args = ap.parse_args()

    Handler.vrm_path = args.vrm
    Handler.report = None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=CLIENT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    profile = os.path.join(TMP, "riko-wavetest-profile")
    subprocess.run(["rm", "-rf", profile], check=False)  # temp profile, never the owner's
    url = f"http://127.0.0.1:{port}/?desktop=1&wavetest=1"
    cmd = [
        "chromium", "--headless=new", f"--user-data-dir={profile}", "--no-sandbox",
        "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
        "--autoplay-policy=no-user-gesture-required", "--window-size=420,680",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--allow-file-access", url,
    ]
    print(f"[wave_check] chromium for {args.secs}s", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        time.sleep(args.secs)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    time.sleep(1.0)  # let an in-flight /wavetest POST land
    srv.shutdown()

    if Handler.report is None:
        print("FAIL: the page never POSTed /wavetest (did it even load?)")
        return 1
    fails = check(Handler.report)
    for f in fails:
        print(f"FAIL: {f}")
    print("PASS: W always makes her wave" if not fails else f"{len(fails)} failure(s)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
