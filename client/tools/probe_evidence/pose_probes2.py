#!/usr/bin/env python3
"""Probe set 2: Y-axis for arms, cookbook pose candidates (T / V / wave /
hands-on-hips), mirrored variants so the same file works on either rig."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent

B = lambda b, v: ("bone", b, list(v))
P = [
    ("lu_yp60", B("leftUpperArm", [0, 60, 0])),
    ("lu_ym60", B("leftUpperArm", [0, -60, 0])),
    ("ll_yp60", B("leftLowerArm", [0, 60, 0])),
    ("ll_ym60", B("leftLowerArm", [0, -60, 0])),
    # T / V pose, both sign pairings (rig mirror makes one of each correct)
    ("T_A", B("leftUpperArm", [0, 0, 70])), ("T_Ab", B("rightUpperArm", [0, 0, -70])),
    ("T_B", B("leftUpperArm", [0, 0, -70])), ("T_Bb", B("rightUpperArm", [0, 0, 70])),
    ("V_A", B("leftUpperArm", [0, 0, 105])), ("V_Ab", B("rightUpperArm", [0, 0, -105])),
    ("V_B", B("leftUpperArm", [0, 0, -105])), ("V_Bb", B("rightUpperArm", [0, 0, 105])),
    # wave candidates: left arm overhead, forearm folded (all sign combos)
    ("WV1", B("leftUpperArm", [0, 0, -95])), ("WV1b", B("leftLowerArm", [0, 90, 0])),
    ("WV2", B("leftUpperArm", [0, 0, -95])), ("WV2b", B("leftLowerArm", [0, -90, 0])),
    ("WV3", B("leftUpperArm", [0, 0, 95])), ("WV3b", B("leftLowerArm", [0, 90, 0])),
    ("WV4", B("leftUpperArm", [0, 0, 95])), ("WV4b", B("leftLowerArm", [0, -90, 0])),
    # hands-on-hips candidates (elbow back + forearm forward/inward)
    ("HP_A", B("leftUpperArm", [-25, 0, 0])), ("HP_Ab", B("leftLowerArm", [70, 0, -35])),
    ("HP_B", B("leftUpperArm", [25, 0, 0])), ("HP_Bb", B("leftLowerArm", [-70, 0, 35])),
]
# combine paired bones into single probes (a,b) -> one animation each
pairs, keys = [], []
i = 0
while i < len(P):
    a = P[i]
    if i + 1 < len(P) and P[i + 1][0].startswith(a[0]):
        b = P[i + 1]
        bones = {a[1][1]: a[1][2], b[1][1]: b[1][2]}
        pairs.append((a[0], bones))
        i += 2
    else:
        pairs.append((a[0], {a[1][1]: a[1][2]}))
        i += 1

DUR, RAMP, STEP = 1.8, 0.3, 2.4
anims, steps = [], []
for idx, (key, bones) in enumerate(pairs):
    name = f"q{idx:02d}_{key}".lower()
    anims.append({"name": name, "description": f"probe2 {key}",
                  "duration_s": DUR, "loop": False, "ease": "linear",
                  "keyframes": [{"t": 0.0, "bones": {}, "expression": {}},
                                {"t": RAMP, "bones": bones, "expression": {}},
                                {"t": DUR, "bones": bones, "expression": {}}]})
    steps.append(f"custom:{name}@{int(600 + idx * STEP * 1000)}")

secs = [round(3.0 + 0.75 * k, 2)
        for k in range(int((len(pairs) * STEP + 4.5) / 0.75))]
(OUT / "probes2.json").write_text(json.dumps({"animations": anims}))
(OUT / "probes2_map.json").write_text(json.dumps(
    {f"q{idx:02d}_{k}": {"probe_index": idx, "bones": v}
     for idx, (k, v) in enumerate(pairs)}))
print("probes:", len(anims))
print("touch:", ";".join(steps))
print("secs:", ",".join(str(s) for s in secs))
