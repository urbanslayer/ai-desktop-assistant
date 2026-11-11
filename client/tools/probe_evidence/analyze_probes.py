#!/usr/bin/env python3
"""Analyze a pose-probe run: for each probe animation, pick the frame in its
hold window and report how the relevant body-part screen rects moved vs the
rest baseline. Objective direction evidence for the convention table."""
import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
maps = json.loads((Path(__file__).parent / "probes_map.json").read_text())

def centers(met):
    out = {}
    for k, r in (met.get("metrics", met) or {}).items():
        pass
    parts = met.get("parts") or {}
    for k, r in parts.items():
        if isinstance(r, dict):
            out[k] = (r["x"] + r.get("w", 0) / 2, r["y"] + r.get("h", 0) / 2)
        else:
            out[k] = (r[0] + r[2] / 2, r[1] + r[3] / 2)
    return out

frames = {}
baselines = []   # no active custom animation (idle / rest hold)
for j in sorted(run.glob("*.json")):
    try:
        d = json.loads(j.read_text())
    except json.JSONDecodeError:
        continue
    met = d.get("metrics") or {}
    qa = met.get("keyframeQA") or {}
    name = qa.get("name")
    if not name or name == "p00_rest":
        baselines.append(met)
        continue
    # prefer frames at full blend weight, mid-hold
    cur = frames.get(name)
    score = (float(qa.get("w") or 0), float(qa.get("t") or 0))
    if cur is None or score > cur[0]:
        frames[name] = (score, j, met, qa)

base = None
if baselines:
    keys = set().union(*(centers(m).keys() for m in baselines))
    base = {k: (sum(centers(m)[k][0] for m in baselines if k in centers(m))
                / sum(1 for m in baselines if k in centers(m)),
                sum(centers(m)[k][1] for m in baselines if k in centers(m))
                / sum(1 for m in baselines if k in centers(m)))
            for k in keys}
    hyd = [m["hipsYpx"] for m in baselines if m.get("hipsYpx") is not None]
    fyd = [m["feetYpx"] for m in baselines if m.get("feetYpx") is not None]
    base_hy = sum(hyd) / len(hyd) if hyd else None
    base_fy = sum(fyd) / len(fyd) if fyd else None
    print(f"# baseline frames: {len(baselines)}", file=sys.stderr)

def d(part, c):
    if not base or part not in base or part not in c:
        return None
    return round(c[part][0] - base[part][0], 1), round(c[part][1] - base[part][1], 1)

rows = []
mapping = {}
for name, m in sorted(maps.items(), key=lambda kv: kv[1]["probe_index"]):
    if name not in frames:
        rows.append((name, "MISSING", ""))
        continue
    score, j, met, qa = frames[name]
    mapping[name] = str(j.with_suffix(".png"))
    c = centers(met)
    spec = m["spec"]
    bone = spec[1] if spec[0] == "bone" else None
    if bone:
        side = "left" if bone.startswith("left") else "right" if bone.startswith("right") else None
    focus = []
    if bone and "UpperArm" in bone:
        focus = [f"hand_{'l' if side == 'left' else 'r'}",
                 f"arm_{'l' if side == 'left' else 'r'}"]
    elif bone and "LowerArm" in bone:
        focus = [f"hand_{'l' if side == 'left' else 'r'}"]
    elif bone and "Shoulder" in bone:
        focus = [f"arm_{'l' if side == 'left' else 'r'}"]
    elif bone in ("spine", "chest", "upperChest"):
        focus = ["head", "torso", "hips", "hand_l", "hand_r"]
    elif bone == "head":
        focus = ["head"]
    elif bone and "Leg" in bone:
        focus = [f"foot_{'l' if side == 'left' else 'r'}"]
    delta = {p: d(p, c) for p in focus}
    hy = (round(met["hipsYpx"] - base_hy, 1)
          if base_hy is not None and met.get("hipsYpx") is not None else None)
    fy = (round(met["feetYpx"] - base_fy, 1)
          if base_fy is not None and met.get("feetYpx") is not None else None)
    rows.append((name, f"w={score[0]:.2f} t={score[1]:.2f}",
                 json.dumps({"delta_px": delta, "hipsYpx": hy, "feetYpx": fy,
                             "frame": j.name})))

for r in rows:
    print(r[0], "|", r[1], "|", r[2])
mapping["p00_rest"] = mapping.get("p01_lu_azp1", "")
import os
restc = [str(run / "idle-0.png")]
for cand in ("idle-0.png",):
    pass
# rest tile: the earliest baseline png
if baselines:
    for j2 in sorted(run.glob("*.json")):
        try:
            m2 = (json.loads(j2.read_text()).get("metrics") or {})
        except Exception:
            continue
        if not (m2.get("keyframeQA") or {}).get("name"):
            mapping["p00rest"] = str(j2.with_suffix(".png"))
            break
p = run / "frames_map.json"
p.write_text(json.dumps(mapping, indent=1))
print("mapping ->", p, file=sys.stderr)
