#!/usr/bin/env python3
"""World-space analysis of probe-set-2 runs: hand deltas vs rest baseline."""
import json
import sys
from pathlib import Path

P = Path(__file__).resolve().parent
run = P / sys.argv[1]
base_run = P / sys.argv[2]
mapping = json.loads((run / "frames_map.json").read_text())
bmap = json.loads((base_run / "frames_map.json").read_text())

def frame(f):
    return json.loads(Path(f).with_suffix(".json").read_text())

def world(name, m):
    d = frame(m[name])
    return d["bones"]

restname = "p00rest" if "p00rest" in bmap else None
b = world(restname, bmap)
mapz = json.loads((P / "probes2_map.json").read_text())

for name in sorted(mapping, key=lambda k: mapz.get(k, {}).get("probe_index", 99)):
    if name not in mapz:
        continue
    w = world(name, mapping)
    out = {}
    for part in ("leftHand", "rightHand", "head", "hips"):
        if w.get(part) and b.get(part):
            dxy = [round(w[part][i] - b[part][i], 3) for i in range(3)]
            if max(abs(v) for v in dxy) > 0.01:
                out[part] = dxy
    print(name, json.dumps(mapz[name]["bones"]), out)
