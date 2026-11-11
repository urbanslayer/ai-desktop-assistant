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
                                          [--vrm PATH] [--size WxH] [--desktop]

Scenarios: idle | thinking | say | think | wave
  think/wave drive the page with ?force= (the director is silenced and the
  named gesture plays at a fixed time), so captures land at exact pose times.
--force accepts the page's spec directly, e.g. --force "wave@0.5,think@4".

The metrics JSON is printed per capture (distances in metres, computed in
the page against the real rig; chin point = rig.chin from client/js/motion.js
— head bone + (0, -0.5·headLen, +0.35·headLen) with headLen from eye
spacing). Acceptance is RELATIVE to the attached rig (works on any VRM):
  wave:  wavePeakAboveShoulderY > 0.25 * armLen,
         waveLateralSwing > 0.15 * forearmLen,
         waveMaxHandNdcAbsX/Y <= 0.9 (raised hand inside the canvas with
         >= 5 % margin at the default 420x680 window; NDC from the page's
         own camera, so the near/far planes are included),
         hand >= headTop - 0.05 (handX rows show the raise).
  think: handToChin < 0.07 * (headLen / 0.12) — 0.07 m is the flat rule
         measured on the sample avatar (whose headLen comes out 0.12 m from
         eye spacing); scale by head size so big- and small-headed rigs get
         the same relative pose. The classic thinker is a wrist-braced
         pose: the curled fingers touch the chin, the wrist sits a bit
         below it.
         hand/elbow not behind the chest plane (z >= chest.z, slack 0.05),
         handInCanvas / elbowInCanvas true, held at 1 s and 5 s into the
         loop.
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
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLIENT = str(REPO / "client")
DEFAULT_VRM = str(REPO / "character_files" / "sample_avatar.vrm")
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
    "walk": ("walk@0.5", []),  # gait photos: use a wide --size, e.g. 1920x680
    # scroll-zoom smoothness QA: needs --desktop (FAKE_DESK resizes the canvas
    # at gesture boundaries exactly like the real window does) and --gestures.
    # Frame times are RELATIVE TO WAKE; the gesture starts at its startMs.
    "zoom": (None, []),
    # touch & posing QA (docs/TOUCH.md): drive the page with --touch specs
    # (page runTouchSpec grammar) and capture mid-interaction. The grab spec
    # MUST drag the cursor far from rest (reviewer: a 0-px drag proves
    # nothing); evaluate_touch_gates() asserts the hand followed the RAW
    # cursor (or landed on the IK clamp point with the reason printed).
    # grab  e.g. --touch "grab hand_l hand_l hand_l 91 -257 900 2200@1500"
    #        (press the hand_l rect centre, drag 91 px out / 257 px UP over
    #         900 ms, park 2200 ms — one PNG per grab fires EVENT-DRIVEN once
    #         the cursor is steady 800 ms, so no clock-alignment guesswork)
    # sit   e.g. --touch "sit:476@1500;sit:578@9000"
    # pat   e.g. --touch "taps head head head head head 260@400" with capture
    #        times INSIDE the pat hold window (e.g. 1.00,2.60 for taps at
    #        400+260k ms): the gate checks the INSTANTANEOUS head-bone roll at
    #        capture time in 8..12 deg, not the latched run peak.
    "grab": (None, []),
    "sit": (None, []),
    "pat": (None, []),
}


def evaluate_touch_gates(outdir):
    """Parse the per-frame /measure JSONs and print the reviewer's non-circular
    touch gates. Returns the number of FAILed checks (0 = all green).

    Per grab frame (metrics.grabQA, see index.html rikoMeasure):
      spec sanity     rawDistPx >= 60      (the cursor must have LEFT rest)
      reachable       dispPx >= 0.6*rawDistPx  AND  handToRawPx <= 12
      clamped         dispPx >= 80 for far targets (rawDistPx >= 200, i.e. the
                      above-shoulder case; else >= 40), hand lands ON the
                      clamped IK point (handToClampedPx <= 14) and the clamp
                      distance + reason (reach vs torso capsule) are printed
      joint limits    live bone deviation vs rest <= GRAB_LIMITS (+0.5 deg)
      capsule         capsuleClearM >= -0.01 (when within the capsule span)
    Per pat frame: instantaneous patRollDeg (react active at capture time);
    the run-level gate is best |instantaneous| in 8..12 across frames.
    """
    fails = 0
    inst_frames = []
    best = 0.0
    scenario_is_pat = any(Path(outdir).glob("pat-*.json"))
    for j in sorted(Path(outdir).glob("*.json")):
        try:
            met = json.loads(j.read_text()).get("metrics") or {}
        except (json.JSONDecodeError, OSError):
            continue
        for gid, e in sorted((met.get("grabQA") or {}).items()):
            if "dispPx" not in e:
                continue
            checks = [("cursor-left-rest>=60px", e["rawDistPx"] >= 60)]
            reason = ""
            if e.get("reachable", False):
                checks.append(("disp>=60%raw", e["dispPx"] >= 0.6 * e["rawDistPx"]))
                checks.append(("hand->raw<=12px", e["handToRawPx"] <= 12))
            elif e.get("clampReason") == "limits":
                checks.append(("disp>=50%raw", e["dispPx"] >= 0.5 * e["rawDistPx"]))
                htc = e.get("handToClampedPx")
                checks.append(("hand->clamp<=14px", htc is not None and htc <= 14))
                reason = (f"joint-limit clamp: IK solved inside reach but GRAB_LIMITS "
                          f"rotation stopped the arm {e['handToRawPx']}px short of "
                          f"the cursor (boneAngles vs limits in json)")
            elif e.get("clampReason"):
                need = 80 if e["rawDistPx"] >= 200 else 40
                checks.append((f"disp>={need}px", e["dispPx"] >= need))
                htc = e.get("handToClampedPx")
                checks.append(("hand->clamp<=14px", htc is not None and htc <= 14))
                reason = (f"{e['clampReason']} clamp: raw {e.get('rawDistM')}m vs "
                          f"reach {e.get('reachM')}m, raw capsule clear "
                          f"{e.get('rawCapsuleClearM')}m -> hand short of cursor "
                          f"by {e.get('handToRawM', e['handToRawPx'])} "
                          f"(lands {htc}px from the clamped point)")
            else:
                checks.append(("has-reachability-verdict", False))
            side = "left" if gid.endswith("_l") else "right"
            bones = ([f"{side}UpperArm", f"{side}LowerArm"] if gid.startswith(("hand", "arm"))
                     else [f"{side}UpperLeg", f"{side}LowerLeg"] if gid.startswith("foot") else [])
            for bn in bones:
                ang = (e.get("boneAngles") or {}).get(bn)
                lim = (e.get("boneLimitsDeg") or {}).get(bn)
                if ang is not None and lim is not None:
                    checks.append((f"{bn}<=limit", ang <= lim + 0.5))
            if e.get("capsuleClearM") is not None:
                checks.append(("outside-capsule", e["capsuleClearM"] >= -0.01))
            bad = [n for n, ok in checks if not ok]
            fails += len(bad)
            print(f"[grab-gate] {j.name} {gid} "
                  f"{'reachable' if e.get('reachable') else 'clamped:' + str(e.get('clampReason'))} "
                  f"disp={e['dispPx']}px "
                  f"raw={e['rawDistPx']}px hand2raw={e['handToRawPx']}px "
                  f"elbow={e.get('elbowDeg')}deg capClear={e.get('capsuleClearM')}m "
                  f"{'PASS' if not bad else 'FAIL ' + ','.join(bad)}"
                  + (f"  ({reason})" if reason else ""), flush=True)
        pk = met.get("patPeakRollDeg")
        ir = met.get("patRollDeg")
        if ir is not None:
            inst_frames.append((j.name, ir))
            best = max(best, abs(ir))
        if pk or ir is not None:
            print(f"[pat-gate]  {j.name} instantaneous head roll="
                  f"{ir if ir is not None else 'none-active'}deg "
                  f"(latched peak {pk}deg)", flush=True)
    if inst_frames:
        ok = 8.0 <= best <= 12.0
        fails += 0 if ok else 1
        print(f"[pat-gate]  best INSTANTANEOUS roll in captured frames "
              f"= {best:.1f}deg (gate 8..12, measured at capture time — not "
              f"the latched run peak) {'PASS' if ok else 'FAIL'}", flush=True)
    elif scenario_is_pat:
        fails += 1
        print("[pat-gate]  FAIL: no captured frame had an active headTilt "
              "react — capture times must land inside the pat window", flush=True)
    return fails


class Handler(SimpleHTTPRequestHandler):
    t0 = time.monotonic()
    queue = []
    outdir = "."
    scenario = "idle"
    shots = 0
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
        m = re.match(r"/(capture|measure)\?t=([\d.]+)", self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if not m:
            # record control-plane POSTs (/control/touch, /control/sit-state,
            # /control/bbox …) so the harness run doubles as seam evidence
            if self.path.startswith("/control/"):
                with open(os.path.join(self.outdir, "touch-log.jsonl"), "a") as f:
                    f.write(json.dumps({"t": round(time.monotonic() - self.t0, 2),
                                        "path": self.path, "body": body[:2000]}) + "\n")
            return self._json({"ok": True})
        kind, t = m.group(1), float(m.group(2))
        stem = f"{self.scenario}-t{t:06.2f}s-{self.shots}"
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
    ap.add_argument("--vrm", default=DEFAULT_VRM,
                    help="VRM file to serve as /avatar.vrm (default: character_files/sample_avatar.vrm)")
    ap.add_argument("--size", default="420x680",
                    help="viewport WxH (default 420x680 = the desktop window)")
    ap.add_argument("--desktop", action="store_true",
                    help="open the page with ?desktop=1 (the desktop.py framing)")
    ap.add_argument("--gaze", default=None,
                    help="page ?gaze= offset 'yaw,pitch' in degrees (forced gaze)")
    ap.add_argument("--gestures", default=None,
                    help="page ?gestures= 'startMs:count:deltaY:gapMs[;...]' — synthetic "
                         "wheel gesture(s) on the canvas, ';' separates bursts (zoom scenario)")
    ap.add_argument("--touch", default=None,
                    help="page ?touch= scripted touch QA steps (grab/tap/taps/stroke/sit/"
                         "stand/mode ops, '@ms' anchors after avatar attach) — see runTouchSpec")
    ap.add_argument("--roam", action="store_true",
                    help="open the page with ?roam=1 (strip mode: walk director + "
                         "/control/bbox housekeeping active)")
    args = ap.parse_args()

    force, events = SCENARIOS[args.scenario]
    if args.force:
        force = args.force
    w, h = (int(v) for v in args.size.lower().split("x"))
    os.makedirs(args.outdir, exist_ok=True)
    Handler.scenario = args.scenario
    Handler.outdir = args.outdir
    Handler.queue = list(events)
    Handler.vrm_path = args.vrm
    Handler.t0 = time.monotonic()  # server clock vs page clock differ a bit; fine
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # fresh port per run: strays from an old run hit nothing
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=CLIENT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    profile = os.path.join(TMP, "riko-chrome-profile")
    subprocess.run(["rm", "-rf", profile], check=False)  # temp profile, never the owner's
    url = f"http://127.0.0.1:{port}/?capture={args.secs}"
    if args.desktop or args.scenario == "zoom":
        url += "&desktop=1"
    if args.roam:
        url += "&roam=1"
    if force:
        url += f"&force={force}"
    if args.gaze:
        url += f"&gaze={args.gaze}"
    if args.gestures:
        url += f"&wheelzoom={args.gestures}"
    if args.touch:
        url += "&touch=" + urllib.parse.quote(args.touch, safe="")
    maxsec = max(float(s) for s in args.secs.split(","))
    dur = args.runtime if args.runtime else maxsec + 8
    cmd = [
        "chromium", "--headless=new", f"--user-data-dir={profile}", "--no-sandbox",
        "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
        "--autoplay-policy=no-user-gesture-required", f"--window-size={w},{h}",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--enable-logging=stderr",
        "--allow-file-access", url,
    ]
    print(f"[harness] chromium for {dur}s -> {args.outdir}", flush=True)
    errlog = open(os.path.join(args.outdir, "chromium.log"), "wb")  # console/stderr for debugging
    # own process group: chromium forks children that survive a plain proc.kill()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errlog,
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
    gate_fails = evaluate_touch_gates(args.outdir)
    print(f"[harness] scenario={args.scenario} shots={len(shots)} touch-gate-fails={gate_fails}")
    for s in shots:
        print("  ", os.path.join(args.outdir, s))
    return 0 if shots and gate_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
