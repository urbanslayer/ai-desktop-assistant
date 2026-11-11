#!/usr/bin/env python3
"""Probe set 3: head/spine directions with forced neutral gaze (--gaze 0,0).
Reads come from eyesWorld/head/hand WORLD coords in frame JSONs."""
import json
import sys

NAMES = [
    ("rest", None),
    # head pitch / yaw / roll
    ("hx_p", ("head", [25, 0, 0])),
    ("hx_m", ("head", [-25, 0, 0])),
    ("hy_p", ("head", [0, 25, 0])),
    ("hy_m", ("head", [0, -25, 0])),
    ("hz_p", ("head", [0, 0, 25])),
    ("hz_m", ("head", [0, 0, -25])),
    # spine / chest / upperChest lateral lean (Z) — which way does the head go?
    ("sp_zp", ("spine", [0, 0, 14])),
    ("ch_zp", ("chest", [0, 0, 14])),
    ("uc_zp", ("upperChest", [0, 0, 14])),
    ("sp_xp", ("spine", [14, 0, 0])),
    # settle arm signs in world coords (mirrored pair test)
    ("la_up", ("leftUpperArm", [0, 0, -70])),
    ("la_dn", ("leftUpperArm", [0, 0, 70])),
    ("ra_up", ("rightUpperArm", [0, 0, 70])),
    ("ra_dn", ("rightUpperArm", [0, 0, -70])),
]
STEP = 3.0
items, secs, touches = [], ["0.3"], []
for i, (key, bone) in enumerate(NAMES):
    name = f"r{i:02d}_{key}".lower()
    kfs = [{"t": 0.0, "bones": {}, "expression": {}},
           {"t": 1.0, "bones": ({bone[0]: bone[1]} if bone else {}), "expression": {}},
           {"t": 2.0, "bones": {}, "expression": {}}]
    items.append({"name": name, "duration_s": 2.0, "loop": False, "ease": "smooth",
                  "keyframes": kfs})
    base = 0.6 + i * STEP
    secs += [f"{base + 1.3:.1f}", f"{base + 2.2:.1f}"]
    touches.append(f"custom:{name}@{int(base * 1000)}")
secs += ["0.8", "1.6"]
json.dump(items, open(sys.argv[1] + "/probes3.json", "w"))
print("secs:", ",".join(sorted(set(secs))))
print("touch:", ";".join(touches))
