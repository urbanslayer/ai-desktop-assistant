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
import math
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
    # custom keyframe animation QA (docs/SELF_ANIMATION.md): serve
    # --animations (default: the victory fixture) as /custom-animations.json,
    # play it with --touch "custom:<name>@<ms>" and photograph at the keys.
    # evaluate_custom_gates() re-samples the fixture IN PYTHON and compares
    # every captured frame's bone offsets AND world rotations (<= 3 deg).
    "custom": (None, []),
    # director path QA (live bug: reply action naming a custom animation was
    # dropped with "unknown action — ignored"): NO --touch here. A queued
    # 'say' event whose actions array NAMES the served animation must play
    # it through director.say -> applyAction's custom catch. Shots fire
    # EVENT-DRIVEN off keyframe progress (&kfwatch=0.35,0.8), so the frame
    # always shows her mid-pose regardless of model load latency.
    "custom_say": (None, []),
    # the same run with the animation delivered ONLY inline on the reply
    # action (the live curtsey_twirl fix): /custom-animations.json serves an
    # empty list, so the custom gate can only pass if the page registers the
    # inline copy before director.say() validates the name.
    "custom_say_inline": (None, []),
    # song ticker (docs/SPOTIFY_NOTES.md): a music:state command arrives at
    # 6 s; the gate checks metrics.tickerQA in the measure dumps — shown,
    # exact text, marquee iff long title, bar above the feet rects.
    "ticker": (None, []),
    "ticker_short": (None, []),
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


def evaluate_custom_gates(outdir, anims):
    """Independent PYTHON re-implementation of the keyframe sampler
    (docs/SELF_ANIMATION.md): for every captured frame that shows an active
    custom animation, recompute the expected rest-relative offset per keyed
    bone (and the composed WORLD rotation from the reported local-rest chain)
    at the frame's own reported (t, w), and gate it to <= 3 deg. The numbers
    under test come from the browser's three.js pipeline, the expectation
    from stdlib math — shared code only through the JSON contract.
    Returns the number of failed checks.
    """
    by_name = {a.get("name"): a for a in anims}
    def q_xyz(d):                      # XYZ Euler degrees -> unit quaternion
        hx, hy, hz = (math.radians(v) / 2 for v in d)
        sx, cx, sy, cy, sz, cz = (math.sin(hx), math.cos(hx), math.sin(hy),
                                  math.cos(hy), math.sin(hz), math.cos(hz))
        return (sx * cy * cz + cx * sy * sz, cx * sy * cz - sx * cy * sz,
                cx * cy * sz + sx * sy * cz, cx * cy * cz - sx * sy * sz)

    def qmul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return (aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw,
                aw * bw - ax * bx - ay * by - az * bz)

    def qnorm(q):
        n = math.sqrt(sum(v * v for v in q))
        return tuple(v / n for v in q) if n > 0 else (0.0, 0.0, 0.0, 1.0)

    def qslerp(a, b, u):
        if u <= 0:
            return a
        if u >= 1:
            return b
        x1, y1, z1, w1 = a
        x2, y2, z2, w2 = b
        c = w1 * w2 + x1 * x2 + y1 * y2 + z1 * z2
        if c < 0:
            x2, y2, z2, w2, c = -x2, -y2, -z2, -w2, -c
        if c > 0.9995:
            return qnorm(tuple(v1 + (v2 - v1) * u for v1, v2 in
                               zip(a, (x2, y2, z2, w2))))
        s = math.sqrt(1 - c * c)
        ang = math.atan2(s, c)
        k1, k2 = math.sin((1 - u) * ang) / s, math.sin(u * ang) / s
        return tuple(v1 * k1 + v2 * k2 for v1, v2 in
                     zip(a, (x2, y2, z2, w2)))

    def qang(a, b):
        d = abs(sum(v1 * v2 for v1, v2 in zip(a, b)))
        return 2 * math.degrees(math.acos(min(1.0, d)))

    ID = (0.0, 0.0, 0.0, 1.0)

    def prep(anim):
        keys = anim["keyframes"]
        dur = float(anim["duration_s"])
        smooth = anim.get("ease", "smooth") == "smooth"

        def ease(u):
            return u * u * (3.0 - 2.0 * u) if smooth else u

        def sample(t):
            """bone -> expected rest-relative offset quaternion at anim-time t."""
            if anim.get("loop"):
                t %= dur
            t = max(0.0, min(dur, t))
            i = 0
            while i + 2 < len(keys) and t >= keys[i + 1]["t"]:
                i += 1
            ka, kb = keys[i], keys[i + 1]
            u = 0.0 if kb["t"] <= ka["t"] else (t - ka["t"]) / (kb["t"] - ka["t"])
            e = ease(u)
            out = {}
            bones = set(ka.get("bones") or {}) | set(kb.get("bones") or {})
            for b in bones:
                va = (ka.get("bones") or {}).get(b, [0.0, 0.0, 0.0])
                vb = (kb.get("bones") or {}).get(b, [0.0, 0.0, 0.0])
                q = qslerp(q_xyz(va), q_xyz(vb), e)
                if qang(q, ID) > 1e-4:
                    out[b] = qnorm(q)
            hy = ((ka.get("hips_y") or 0.0) * (1 - e)
                  + (kb.get("hips_y") or 0.0) * e)
            return out, hy
        return sample, ease

    prepped = {}
    fails = 0
    frames = 0
    for j in sorted(Path(outdir).glob("*.json")):
        try:
            met = json.loads(j.read_text()).get("metrics") or {}
        except (json.JSONDecodeError, OSError):
            continue
        qa = met.get("keyframeQA")
        if not qa:
            continue
        anim = by_name.get(qa.get("name"))
        if anim is None:
            fails += 1
            frames += 1
            print(f"[custom-gate] {j.name} FAIL: animation name "
                  f"{qa.get('name')!r} is not in the served list", flush=True)
            continue
        if id(anim) not in prepped:
            prepped[id(anim)] = prep(anim)
        sample, ease = prepped[id(anim)]
        frames += 1
        t, w = float(qa["t"]), float(qa["w"])
        blend = ease(min(1.0, max(0.0, w)))
        off_q, hips = sample(t)
        checks = [(f"name={anim['name']}", qa.get("name") == anim["name"]),
                  ("neck-never-keyed", "neck" not in (qa.get("bones") or {}))]
        worst = 0.0
        for bone, e in sorted((qa.get("bones") or {}).items()):
            if bone not in off_q:      # page wrote rest there; assert it holds
                got = tuple(e.get("q") or ID)
                checks.append((f"{bone}-at-rest", qang(got, ID) <= 3.0))
                continue
            want = qslerp(ID, off_q[bone], blend)   # pose blends by power e
            err = qang(want, tuple(e["q"]))
            worst = max(worst, err)
            checks.append((f"{bone}-offset<=3deg", err <= 3.0))
            # WORLD check without any matrix decompose: the page reports the
            # live LOCAL quaternion of every ancestor up to vrm.scene
            # (chainQ, hips-side first). Actual = product of those; expected
            # = same product with keyed nodes replaced by rest * offset — a
            # limb's rest-relative angle must survive the whole parent chain
            # (a wrong spine would throw the arm off). Live local rotations
            # are immune to the negative bone scales Unity rigs carry, which
            # scramble getWorldQuaternion decompose (measured on Furina).
            chain = (qa.get("chain") or {}).get(bone)
            chainq = (qa.get("chainQ") or {}).get(bone)
            if chain and chainq and len(chain) == len(chainq):
                node_as_bone = {}
                for b2, e2 in (qa.get("bones") or {}).items():
                    c2 = (qa.get("chain") or {}).get(b2)
                    if c2:
                        node_as_bone[c2[-1]] = b2
                act = exp = ID
                for idx, node in enumerate(chain):
                    live = qnorm(tuple(chainq[idx]))
                    act = qmul(act, live)
                    b2 = node_as_bone.get(node)
                    if b2 is None:
                        exp = qmul(exp, live)   # unkeyed: live is its own rest
                        continue
                    r = (qa["bones"][b2] or {}).get("rest")
                    base = qnorm(tuple(r)) if r else ID
                    o = off_q.get(b2)
                    exp = qmul(exp, base if o is None
                               else qmul(base, qslerp(ID, o, blend)))
                werr = qang(exp, act)
                worst = max(worst, werr)
                checks.append((f"{bone}-world<=3deg", werr <= 3.0))
        got_hips = float(qa.get("hipsY") or 0.0)
        checks.append((f"hipsY={hips * blend:.3f}",
                       abs(got_hips - hips * blend) <= 0.011))
        bad = [n for n, ok in checks if not ok]
        fails += len(bad)
        print(f"[custom-gate] {j.name} name={qa.get('name')} t={t:.3f} w={w:.2f} "
              f"bones={len(qa.get('bones') or {})} worstErr={worst:.2f}deg "
              f"hipsY={got_hips:.4f} {'PASS' if not bad else 'FAIL ' + ','.join(bad)}",
              flush=True)
    if not frames:
        fails += 1
        print("[custom-gate] FAIL: no captured frame had an active custom "
              "animation — check the --touch custom:<name>@ms step and the "
              "capture times", flush=True)
    return fails


def evaluate_ticker_gates(outdir, expect_marquee, expect_text):
    """Song-ticker gates off metrics.tickerQA in the /measure dumps (the
    page-side painter state, docs/SPOTIFY_NOTES.md):
      shown           at least one frame shows the bar
      text            the rendered text is exactly 'track — artists'
      marquee         scrolls iff the title overflows (expect_marquee)
      button          btn rect sits inside the bar rect, icon state 'pause'
      feet clearance  no feet part rect intersects the bar rect (the ticker
                      never covers her feet hit rects)
    Returns the number of FAILed checks.
    """
    fails = 0
    hit = None
    for j in sorted(Path(outdir).glob("*.json")):
        try:
            met = json.loads(j.read_text()).get("metrics") or {}
        except (json.JSONDecodeError, OSError):
            continue
        qa = met.get("tickerQA") or {}
        if qa.get("shown"):
            hit = (j.name, qa, met)
            break
    if not hit:
        print("[ticker-gate] FAIL: no captured frame showed the ticker")
        return 1
    name, qa, met = hit
    checks = [("text", qa.get("text") == expect_text),
              ("marquee", qa.get("marquee") is expect_marquee),
              ("btn-state-pause", qa.get("btnState") == "pause")]
    bx, by, bw, bh = qa.get("bar") or [0, 0, 0, 0]
    checks.append(("bar-slim-bottom", bh <= 34 and by >= 400))
    kx, ky, kw, kh = qa.get("btn") or [0, 0, 0, 0]
    checks.append(("btn-inside-bar", bx <= kx and ky >= by and kx + kw <= bx + bw
                   and ky + kh <= by + bh))
    for part in ("foot_l", "foot_r"):
        r = (met.get("parts") or {}).get(part)
        if not r:
            continue
        if isinstance(r, dict):
            rx, ry, rw, rh = (r["x"], r["y"],
                              r.get("w", r.get("width", 0)),
                              r.get("h", r.get("height", 0)))
        else:
            rx, ry, rw, rh = r[:4]
        overlap = not (rx + rw <= bx or bx + bw <= rx
                       or ry + rh <= by or by + bh <= ry)
        # parts rects carry a 6 px visual pad; a <= 6 px nick of the pad by
        # a pointer-events:none bar is not "covering the feet hit rect"
        nick = max(0.0, min(ry + rh, by + bh) - max(ry, by)) if overlap else 0.0
        checks.append((f"{part}-clears-bar", not overlap or nick <= 6))
        if overlap and nick <= 6:
            print(f"[ticker-gate] note: {part} pad nicked by {nick:.0f}px "
                  "(bar top touches the rect's 6px pad; pointer-events: "
                  "none, no touch is stolen)")
    bad = [n for n, ok in checks if not ok]
    print(f"[ticker-gate] {name} text={qa.get('text')!r} marquee="
          f"{qa.get('marquee')} bar={qa.get('bar')} btn={qa.get('btn')} "
          + ("OK" if not bad else f"FAIL: {bad}"))
    fails += len(bad)
    return fails


class Handler(SimpleHTTPRequestHandler):
    t0 = time.monotonic()
    queue = []
    outdir = "."
    scenario = "idle"
    shots = 0
    vrm_path = DEFAULT_VRM
    anim_path = str(REPO / "client" / "js" / "fixtures" / "animation_victory.json")
    anim_doc = {"animations": []}
    serve_custom = True   # False -> /custom-animations.json serves an empty list

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/settings":
            return self._json({})
        if path == "/animations.json":
            return self._json({"animations": []})
        if path == "/custom-animations.json":
            if not self.serve_custom:
                # custom_say_inline: the desktop cache never saw the dance —
                # the reply action's inline copy is the only source
                return self._json({"animations": []})
            with open(self.anim_path) as f:
                anim = json.load(f)
            return self._json(anim if "animations" in anim else {"animations": [anim]})
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
        # Handler.queue is a CLASS attr and ThreadingHTTPServer makes a fresh
        # instance per request: pruning through `self.queue` only shadowed it,
        # so every /next poll re-delivered past events (82 duplicate 'say's
        # kept the bubble up forever). Prune the class attr itself.
        Handler.queue = [(at, ev) for (at, ev) in Handler.queue if at > now]
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
    ap.add_argument("--animations", default=None,
                    help="json file (one animation or {animations:[...]}) served "
                         "as /custom-animations.json (default: the victory fixture)")
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
    if args.animations:
        Handler.anim_path = args.animations
    with open(Handler.anim_path) as f:
        _anim_raw = json.load(f)
    Handler.anim_doc = _anim_raw if "animations" in _anim_raw else {"animations": [_anim_raw]}
    if args.scenario in ("custom_say", "custom_say_inline"):
        # prove the DIRECTOR path: the hub's reply action names the custom
        _first = Handler.anim_doc["animations"][0]
        # long text on purpose: the bubble then really covers her head band,
        # so the same run doubles as bubble context for touch QA (TOUCH.md)
        _text = ("Look what I made! This reply line is deliberately long so "
                 "the speech bubble stretches well past her head, the way a "
                 "wordy answer does in the real desktop window: press a part "
                 "under it and the bubble should not eat your click anymore.")
        _action = {"name": _first["name"], "at": 0.2}
        if args.scenario == "custom_say_inline":
            Handler.serve_custom = False
            _action.update({"type": "gesture", "custom": True,
                            "preview": True, "animation": _first})
        Handler.queue = [
            (6.0, {"type": "status", "state": "speaking"}),
            (6.5, {"type": "say", "text": _text, "emotion": "happy",
                   "seconds": 3, "actions": [_action]}),
        ]
    if args.scenario in ("ticker", "ticker_short"):
        # desktop forwards the hub's now_playing as a command event; feed the
        # page the real envelope (session._emit_music_state's shape)
        long_title = args.scenario == "ticker"
        _text = ""
        _snap = {
            "playing": True, "uri": "spotify:track:capTICKER001",
            "track": ("Bohemian Rhapsody (2011 Remaster) · Guitar Immortal "
                      "Version with the full operatic middle section"
                      if long_title else "Call Me"),
            "artists": "Queen & Friends" if long_title else "Riko",
            "album": "A Night at the Opera", "duration_ms": 355000,
            "position_ms": 60000, "device": "Riko", "context_uri": "",
        }
        _text = f"{_snap['track']} — {_snap['artists']}"
        Handler.queue = [(6.0, {"type": "command",
                                "cmd": "music:state "
                                       + json.dumps(_snap, ensure_ascii=False)})]
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
    if args.scenario in ("custom_say", "custom_say_inline"):
        url += "&kfwatch=0.35,0.8"
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
    if args.scenario in ("custom", "custom_say", "custom_say_inline"):
        gate_fails += evaluate_custom_gates(args.outdir, Handler.anim_doc["animations"])
    if args.scenario in ("ticker", "ticker_short"):
        gate_fails += evaluate_ticker_gates(args.outdir, args.scenario == "ticker",
                                            _text)
    print(f"[harness] scenario={args.scenario} shots={len(shots)} touch-gate-fails={gate_fails}")
    for s in shots:
        print("  ", os.path.join(args.outdir, s))
    return 0 if shots and gate_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
