#!/usr/bin/env python3
"""Generate the pose-convention probe set + the capture_frames.py command.

Each probe holds ONE bone at ONE signed axis value (or hips_y) so the
captured frames measure what each axis sign does visually vs the page rest
pose. Probes are explicit at every key (identical under old/new semantics),
so the custom-gate stays green either way.
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent

# (probe key, kind, value)  kind: ('bone', bone, [x,y,z]) | ('hips_y', v) | ('rest',)
P = [("rest", ("rest",))]
for ax in ("z", "x"):
    for sgn in (1, -1):
        P.append((f"lu_a{ax}{sgn:+d}".replace("+", "p").replace("-", "m"),
                  ("bone", "leftUpperArm", [60 * sgn if ax == "x" else 0, 0,
                                            60 * sgn if ax == "z" else 0])))
for ax in ("z", "x"):
    for sgn in (1, -1):
        P.append((f"ll_a{ax}{sgn:+d}".replace("+", "p").replace("-", "m"),
                  ("bone", "leftLowerArm", [60 * sgn if ax == "x" else 0, 0,
                                            60 * sgn if ax == "z" else 0])))
for ax in ("z", "y", "x"):
    P.append((f"lsh_{ax}p30",
              ("bone", "leftShoulder", [30 if ax == "x" else 0,
                                        30 if ax == "y" else 0,
                                        30 if ax == "z" else 0])))
for sgn in (-1, 1):
    P.append((f"ru_az{sgn:+d}".replace("+", "p").replace("-", "m"),
              ("bone", "rightUpperArm", [0, 0, 60 * sgn])))
P += [("sp_yp15", ("bone", "spine", [0, 15, 0])),
      ("sp_zp15", ("bone", "spine", [0, 0, 15])),
      ("sp_zm15", ("bone", "spine", [0, 0, -15])),
      ("sp_xp15", ("bone", "spine", [15, 0, 0])),
      ("sp_xm15", ("bone", "spine", [-15, 0, 0])),
      ("ch_yp15", ("bone", "chest", [0, 15, 0])),
      ("ch_zp15", ("bone", "chest", [0, 0, 15])),
      ("uc_zp15", ("bone", "upperChest", [0, 0, 15])),
      ("hd_xp30", ("bone", "head", [30, 0, 0])),
      ("hd_xm30", ("bone", "head", [-30, 0, 0])),
      ("hd_yp30", ("bone", "head", [0, 30, 0])),
      ("hd_ym30", ("bone", "head", [0, -30, 0])),
      ("hd_zp30", ("bone", "head", [0, 0, 30])),
      ("hd_zm30", ("bone", "head", [0, 0, -30])),
      ("ul_xp60", ("bone", "leftUpperLeg", [60, 0, 0])),
      ("ul_xm60", ("bone", "leftUpperLeg", [-60, 0, 0])),
      ("ll_xp90", ("bone", "leftLowerLeg", [90, 0, 0])),
      ("ll_xm90", ("bone", "leftLowerLeg", [-90, 0, 0])),
      ("hy_up10", ("hips_y", 0.10)),
      ("hy_dn10", ("hips_y", -0.10))]

DUR, RAMP, STEP = 1.8, 0.3, 2.4
anims, steps = [], []
for i, (key, spec) in enumerate(P):
    name = f"p{i:02d}_{key}"
    bones, hy = {}, None
    if spec[0] == "bone":
        bones = {spec[1]: spec[2]}
    elif spec[0] == "hips_y":
        hy = spec[1]
    mid = {"t": RAMP, "bones": bones, "expression": {}}
    last = {"t": DUR, "bones": bones, "expression": {}}
    if hy is not None:
        mid["hips_y"] = hy
        last["hips_y"] = hy
    anims.append({"name": name, "description": f"probe {key}",
                  "duration_s": DUR, "loop": False, "ease": "linear",
                  "keyframes": [{"t": 0.0, "bones": {}, "expression": {}},
                                mid, last]})
    steps.append(f"custom:{name}@{int(600 + i * STEP * 1000)}")

secs = [round(3.0 + 0.75 * k, 2)
        for k in range(int((len(P) * STEP + 4.5) / 0.75))]
(OUT / "probes.json").write_text(json.dumps({"animations": anims}))
(OUT / "probes_map.json").write_text(json.dumps(
    {f"p{i:02d}_{k}": {"probe_index": i, "spec": list(s) if not isinstance(s[0], str) else s}
     for i, (k, s) in enumerate(P)}))
print("probes:", len(anims))
print("touch:", ";".join(steps))
print("secs:", ",".join(str(s) for s in secs))
