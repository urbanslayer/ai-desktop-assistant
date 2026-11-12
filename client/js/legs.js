// legs.js — automatic hop legs for hips_y-driven keyframe animations
// (docs/SELF_ANIMATION.md, "Hops bend the legs" contract). Pure module: no
// three.js, plain [x,y,z] world-space arrays — runs unchanged in the page,
// in `node --test`, and in any harness.
//
// Why: a hop that only keys hips_y rises and lands with STRAIGHT legs. The
// player therefore derives the six leg bones from hips_y every frame (a doc
// that keys a leg bone always wins). Everything is solved in WORLD space
// from the attached rig's rest joint positions — bone local axis signs are
// per-rig (the cookbook in docs/SELF_ANIMATION.md proves the sample avatar
// is axis-mirrored vs the live model), so nothing here encodes an Euler
// convention; the pose layer (pose.js _autoLegs) turns these world targets
// into local rotations by pre-multiplying the REST WORLD quaternion.
//
// Contract behaviour (docs/SELF_ANIMATION.md):
//   hips_y < 0 (crouch / landing): FEET PLANTED — two-bone IK keeps each
//     foot at its rest world position while the hips sink, knees bend
//     toward their rest pole (straight rest legs: pole = character
//     forward, i.e. knees forward). If the dip is deeper than the joint
//     limits allow, the knee bends to its limit and the FOOT SINKS rather
//     than the leg over-rotating.
//   hips_y > 0 (airborne): feet ride with the body PLUS a tuck — knee flex
//     eases smoothly to ~95° at hips_y >= 0.10, thigh forward = 0.3 * knee,
//     the foot rides with the shin and points ~15° beyond it; L/R differ by
//     ~10 % so it does not look mechanical.
//   Both branches agree at hips_y = 0: exact rest (no pop).
//
// Geometric joint-limit clamps mirror the page's KEYFRAME_LIMITS angles
// (leftUpperLeg/rightUpperLeg 103°, leftLowerLeg/rightLowerLeg 126°); the
// pose layer additionally clamps the written local offset into the
// per-axis euler box before applying it.

import { sub, add, mul, dot, len, norm, dist, solveTwoBoneIK } from './ik.js';

const DEG = Math.PI / 180;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smoothstep = (u) => u * u * (3 - 2 * u);
const UP = [0, 1, 0];
const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];

// ── contract numbers (tune here, not in the caller) ──────────
export const HOP_LEGS = {
  kneeDeg: 95,          // knee flex at full tuck (apex)
  fullY: 0.10,          // hips_y where the tuck is full
  thighFrac: 0.30,      // thigh forward = 0.3 * knee flex
  footPointDeg: 15,     // toe point beyond the shin line at full tuck
  asym: 0.10,           // L/R flex difference (fraction)
  kneeMaxDeg: 126,      // == KEYFRAME_LIMITS.*LowerLeg geometric cap
  thighMaxDeg: 103,     // == KEYFRAME_LIMITS.*UpperLeg geometric cap
};

/** Rotate v by angle (rad) around a UNIT axis (Rodrigues). */
export function rotAxis(v, axis, angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  const k = cross(axis, v);
  const d = dot(axis, v);
  return add(add(mul(v, c), mul(k, s)), mul(axis, d * (1 - c)));
}

/** Horizontal unit forward (falls back to +Z), and the sagittal hinge axis:
 *  normalize(forward x up) points toward the character's RIGHT, so rotating
 *  a downward-pointing bone by a POSITIVE angle about it swings that bone
 *  FORWARD — for any facing, on any rig, with no Euler convention. */
export function hopFrame(forward) {
  const f = forward ? [forward[0], 0, forward[2]] : [0, 0, 1];
  const fl = len(f);
  const fu = fl > 1e-6 ? mul(f, 1 / fl) : [0, 0, 1];
  return { forward: fu, axis: norm(cross(fu, UP)) };
}

/**
 * Solve ONE leg for one frame.
 *   hip, knee, ankle — WORLD rest joint positions of this rig (attach time,
 *                      legs at rest; the Foot bone origin is the ankle)
 *   forward          — character facing (world, horizontal; any y ignored)
 *   hipsY            — the BLEND-SCALED hips_y for this frame (metres)
 *   hipLive          — optional: this frame's ACTUAL hip-joint world position
 *                      (hips_y already applied, idle sway included). The
 *                      life layer rotates the hips every frame, so solving
 *                      from the attach-time rest hip would hand that sway to
 *                      the "planted" foot (measured: up to 2.4 cm of xz
 *                      drift on the owner's rig). With hipLive the chain is
 *                      rooted where the hip actually is, and the planted
 *                      target blends from passive (sway-following, at
 *                      hips_y=0 — exact continuity) to fully pinned at the
 *                      rest world position by |hips_y| = 0.01.
 *   sideScale        — per-side tuck multiplier (HOP_LEGS.asym L/R flavour)
 * Returns null when the chain is degenerate (rig without usable legs).
 *
 * -> { mode: 'planted'|'tuck',
 *      thighDir, shinDir      unit WORLD directions the two bones must point
 *      kneePos, anklePos      resulting WORLD joint positions (anklePos =
 *                             where the foot bone origin lands)
 *      footArc                {axis, angle}: world axis-angle to PRE-MULTIPLY
 *                             onto the foot bone's rest world quaternion
 *                             (0 angle = leave the foot flat)
 *      kneeFlexDeg, thighFlexDeg, thighAngle, shinAngle }  // metrics
 */
export function solveHopLeg({ hip, knee, ankle, forward, hipsY,
                              hipLive = null, sideScale = 1, limits = HOP_LEGS }) {
  if (!hip || !knee || !ankle || !Number.isFinite(hipsY)) return null;
  const l1 = dist(hip, knee), l2 = dist(knee, ankle);
  if (!(l1 > 0.05 && l2 > 0.05)) return null;
  const { forward: f, axis } = hopFrame(forward);
  const u0 = norm(sub(knee, hip));            // rest thigh world direction
  const w0 = norm(sub(ankle, knee));          // rest shin world direction
  const H0 = [hip[0], hip[1] + hipsY, hip[2]]; // hips at this frame's offset
  const H = hipLive ? [hipLive[0], hipLive[1], hipLive[2]] : H0;
  const sway = hipLive ? sub(H, H0) : [0, 0, 0];  // non-hips_y hip offset
  let mode, thighDir, shinDir, kneePos, anklePos, footArc, thighAngle = null, shinAngle = null;

  if (hipsY >= 0) {
    // ── airborne: ride + tuck (feet leave the ground WITH the body) ──
    mode = 'tuck';
    const tf = clamp(sideScale, 0, 2) * smoothstep(clamp(hipsY / limits.fullY, 0, 1));
    const phi = limits.kneeDeg * DEG * tf;    // knee flex target
    const th = limits.thighFrac * phi;        // thigh forward
    const gl = th - phi;                      // shin world swing (negative = heel back)
    thighDir = rotAxis(u0, axis, th);
    shinDir = rotAxis(w0, axis, gl);
    kneePos = add(H, mul(thighDir, l1));
    anklePos = add(kneePos, mul(shinDir, l2));
    thighAngle = th;
    shinAngle = gl;
    // foot rides with the shin and POINTS beyond it (toe drops as the shin
    // swings back; +point curls the toe further down = plantar flex)
    footArc = { axis, angle: gl - limits.footPointDeg * DEG * tf };
  } else {
    // ── crouch / landing: feet planted, hips sink, knees forward ──
    mode = 'planted';
    // planted target: rest ankle spot, softened toward the PASSIVE foot
    // (sway-following) as hips_y approaches 0 so the branch is exactly
    // continuous there; fully pinned by |hips_y| = 0.01 (a crouch). Long-
    // legged rigs only reach ~49 deg of flex from a pure 8 cm dip — a real
    // crouch also rolls the weight forward (heels lift), so the ankle
    // target leans back 8 mm as the crouch sets in: +2-3 deg of knee flex
    // for an 8 mm xz shift (the contract drift gate is 2 cm).
    const pin = smoothstep(clamp(-hipsY / 0.010, 0, 1));
    const baseT = add(add(ankle, mul(sway, 1 - pin)), mul(f, -0.008 * pin));
    const d = dist(H, baseT);
    // knee flex the geometry demands for hips->planted-ankle distance d
    const cosB = clamp((d * d - l1 * l1 - l2 * l2) / (2 * l1 * l2), -1, 1);
    let beta = Math.acos(cosB);
    // joint limits: the knee cap is direct; the thigh cap bites when the
    // far segment would swing the thigh past its limit (l2 >> l1 rigs)
    let betaMax = limits.kneeMaxDeg * DEG;
    const s = (d * Math.sin(limits.thighMaxDeg * DEG)) / l2;
    if (s < 1) betaMax = Math.min(betaMax, Math.asin(s));
    let target = baseT;
    if (beta > betaMax) {
      // deeper than the legs can fold: bend ONLY to the limit and let the
      // foot sink below its planted rest height (no over-rotation)
      beta = betaMax;
      const dEff = Math.sqrt(Math.max(0, l1 * l1 + l2 * l2 + 2 * l1 * l2 * Math.cos(betaMax)));
      const dir = norm(sub(baseT, H));
      target = add(H, mul(dir, dEff));
    }
    // pole: the rest knee's perpendicular around the hip->ankle line so a
    // bent-rest rig keeps bending ITS way (and continuity at hips_y=0 is
    // exact); straight rest legs fall back to knees-toward-facing.
    const line = norm(sub(baseT, H));
    const ko = sub(knee, hip);
    let pole = norm(sub(ko, mul(line, dot(ko, line))));
    if (!(len(pole) > 1e-3)) pole = f;
    const sol = solveTwoBoneIK(H, target, l1, l2, pole);
    if (!sol) return null;
    thighDir = sol.dirUpper;
    shinDir = sol.dirLower;
    kneePos = sol.elbow;
    anklePos = sol.wrist;
    footArc = { axis, angle: 0 };             // feet stay flat
  }

  const angDeg = (a, b) => Math.acos(clamp(dot(a, b) / (len(a) * len(b)), -1, 1)) / DEG;
  return {
    mode, thighDir, shinDir, kneePos, anklePos, footArc,
    kneeFlexDeg: angDeg(thighDir, shinDir),   // 0 = straight (matches the
    thighFlexDeg: angDeg(u0, thighDir),       // pose.debug legAngles gate)
    thighAngle, shinAngle,                    // tuck-branch hinge angles (rad)
  };
}

/**
 * Both legs of one rig for one frame.
 *   legs: { left: {hip,knee,ankle}, right: {hip,knee,ankle} }  (missing side
 *         -> that side is null; never throws — rigs may lack bones)
 * Returns { left, right } (each a solveHopLeg result or null). Left keeps
 * full tuck, right flexes (1 - asym) of it: the ~10 % L/R difference the
 * contract asks for so a hop does not read mechanical.
 */
export function solveHopLegs({ legs, forward, hipsY, limits = HOP_LEGS }) {
  const out = { left: null, right: null };
  if (!legs) return out;
  const scales = { left: 1, right: 1 - clamp(limits.asym, 0, 0.5) };
  for (const side of ['left', 'right']) {
    const leg = legs[side];
    if (!leg) continue;
    out[side] = solveHopLeg({ ...leg, forward, hipsY, sideScale: scales[side], limits });
  }
  return out;
}
