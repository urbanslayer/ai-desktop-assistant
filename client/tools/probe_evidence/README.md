# Pose probe evidence (docs/SELF_ANIMATION.md "Measured pose cookbook")

`pose_probes*.py` print a `secs:`/`touch:` spec and write `probes*.json`
(`{"animations": [...]}` — one probe animation per bone/axis/sign, hold pose at t=1.0
via keys rest→pose→rest). Capture (repeat with `--vrm <furina.vrm>` for the live rig):

    python3 tools/capture_frames.py custom <outdir> "<secs>" --animations <probes.json> \
        --touch "<touch>" --gaze 0,0 --size 640x820 [--vrm path/to/furina.vrm]

`analyze_probes.py` / `world2.py` read each frame's `bones`/`metrics` world positions and
print per-probe deltas vs the rest baseline (objective axis-direction evidence). Contact
sheets built from the same runs live in `docs/evidence/pose_measured_*.png`.
