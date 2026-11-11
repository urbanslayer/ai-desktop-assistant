/**
 * keyframes.js — pure keyframe animation math for custom animations
 * (docs/SELF_ANIMATION.md, item 6). No THREE: quaternions are plain
 * {x,y,z,w} objects so node can run this without the vendor bundle; the
 * pose layer (pose.js -> playKeyframes) turns them into bone rotations.
 *
 * Contract (the format is literal, docs/SELF_ANIMATION.md):
 *   {schema:1, name, duration_s: 0.4..8, loop?: bool, ease?: 'smooth'|'linear',
 *    keyframes: [ { t, bones?: {<bone>: [x,y,z] degrees XYZ, rest-relative},
 *                   hips_y?: metres, expression?: {<preset>: 0..1} }, ... ] }
 *   - 2..24 keys, t strictly ascending, first t === 0, last t === duration_s
 *   - a bone absent from a key rests at that key; `neck` is never allowed
 *   - 'smooth' = cubic ease in/out per segment (x²(3-2x), the same curve the
 *     sit/grab blends use), then slerp between the two key quaternions
 *   - rotations are relative to the NORMALIZED bone rest (three-vrm
 *     normalizedBoneQuats); hips_y is a position offset on top of the
 *     normalized hips position
 */

export const KF_BLEND = 0.25;  // s blended in and out (contract-fixed)

// Per-bone axis limits in DEGREES. spine/chest/upperChest ±15 and head ±40
// are the contract's (they mirror motion.js HEAD_LIMITS and the spine
// breath budget); the limb numbers track pose.js GRAB_LIMITS / joint ranges
// so a keyframe can never ask for a rotation a grab would refuse.
const LIM = (deg) => ({ x: [-deg, deg], y: [-deg, deg], z: [-deg, deg] });
export const KEYFRAME_LIMITS = {
  hips: { x: [-30, 30], y: [-45, 45], z: [-30, 30] },
  spine: LIM(15), chest: LIM(15), upperChest: LIM(15),
  head: LIM(40),                        // neck is reserved (gaze/sway layer)
  leftShoulder: LIM(45), rightShoulder: LIM(45),
  leftUpperArm: LIM(137), rightUpperArm: LIM(137),    // pose.js GRAB_LIMITS 2.4 rad
  leftLowerArm: LIM(126), rightLowerArm: LIM(126),    //                      2.2 rad
  leftHand: LIM(90), rightHand: LIM(90),
  leftUpperLeg: LIM(103), rightUpperLeg: LIM(103),    //                      1.8 rad
  leftLowerLeg: LIM(126), rightLowerLeg: LIM(126),    //                      2.2 rad
  leftFoot: LIM(60), rightFoot: LIM(60),
};
// Fingers: leftThumbProximal … rightLittleDistal (three-vrm normalized names;
// rigs without finger bones just keep an open hand, nothing else breaks).
for (const side of ['left', 'right']) {
  KEYFRAME_LIMITS[`${side}ThumbProximal`] = LIM(90);
  KEYFRAME_LIMITS[`${side}ThumbDistal`] = LIM(90);
  for (const finger of ['Index', 'Middle', 'Ring', 'Little']) {
    for (const joint of ['Proximal', 'Intermediate', 'Distal']) {
      KEYFRAME_LIMITS[`${side}${finger}${joint}`] = LIM(90);
    }
  }
}
export const KEYFRAME_BONES = Object.keys(KEYFRAME_LIMITS);

export const KEYFRAME_EXPRESSIONS = [
  'happy', 'angry', 'sad', 'relaxed', 'surprised',
  'aa', 'ih', 'ou', 'ee', 'oh', 'blink',
];

// metres, relative to the normalized hips rest: -0.25 .. 0.15 (contract)
export const HIPS_Y_MIN = -0.25;
export const HIPS_Y_MAX = 0.15;

const DEG = Math.PI / 180;
const NAME_RE = /^[a-z][a-z0-9_]{1,31}$/;

// ── quaternion helpers (plain objects, XYZ order = three.js 'XYZ') ──

/** Euler XYZ degrees -> unit quaternion (same math as THREE.Quaternion.setFromEuler). */
export function eulerToQuatXYZ(xDeg, yDeg, zDeg) {
  const hx = xDeg * DEG / 2, hy = yDeg * DEG / 2, hz = zDeg * DEG / 2;
  const sx = Math.sin(hx), cx = Math.cos(hx);
  const sy = Math.sin(hy), cy = Math.cos(hy);
  const sz = Math.sin(hz), cz = Math.cos(hz);
  return {
    x: sx * cy * cz + cx * sy * sz,
    y: cx * sy * cz - sx * cy * sz,
    z: cx * cy * sz + sx * sy * cz,
    w: cx * cy * cz - sx * sy * sz,
  };
}

export function quatSlerp(a, b, u) {
  if (!(u > 0)) return { ...a };
  if (!(u < 1)) return { ...b };
  let { x: x1, y: y1, z: z1, w: w1 } = a;
  let { x: x2, y: y2, z: z2, w: w2 } = b;
  let cos = w1 * w2 + x1 * x2 + y1 * y2 + z1 * z2;
  if (cos < 0) { x2 = -x2; y2 = -y2; z2 = -z2; w2 = -w2; cos = -cos; }
  if (cos > 0.9995) {           // nearly identical: linear + renormalize
    const x = x1 + (x2 - x1) * u, y = y1 + (y2 - y1) * u;
    const z = z1 + (z2 - z1) * u, w = w1 + (w2 - w1) * u;
    return quatNormalize({ x, y, z, w });
  }
  const sin = Math.sqrt(1 - cos * cos);
  const ang = Math.atan2(sin, cos);
  const sa = Math.sin(ang);
  const k1 = Math.sin((1 - u) * ang) / sa, k2 = Math.sin(u * ang) / sa;
  return {
    x: x1 * k1 + x2 * k2, y: y1 * k1 + y2 * k2,
    z: z1 * k1 + z2 * k2, w: w1 * k1 + w2 * k2,
  };
}

export function quatNormalize(q) {
  const n = Math.hypot(q.x, q.y, q.z, q.w) || 1;
  return { x: q.x / n, y: q.y / n, z: q.z / n, w: q.w / n };
}

export const IDENTITY_QUAT = { x: 0, y: 0, z: 0, w: 1 };

export function quatIsIdentity(q) {
  return Math.abs(q.w) > 1 - 1e-9 &&
    Math.abs(q.x) < 1e-9 && Math.abs(q.y) < 1e-9 && Math.abs(q.z) < 1e-9;
}

/** Angle in degrees between two rotations (quaternion distance). */
export function quatAngleDeg(a, b) {
  let d = a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w;
  d = Math.min(1, Math.abs(d));            // q and -q are the same rotation
  return 2 * Math.acos(Math.min(1, d)) / DEG;
}

/** Cubic ease in/out — the codebase's EASE curve, applied per segment. */
export function easeSmooth(u) { return u * u * (3 - 2 * u); }

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// ── validate / clamp ─────────────────────────────────────────

const isVec3 = (v) => Array.isArray(v) && v.length === 3 &&
  v.every((n) => typeof n === 'number' && Number.isFinite(n));

/** Structural check against the docs/SELF_ANIMATION.md contract.
 *  Returns an array of human-readable errors ([] = valid). The page drops
 *  invalid animations on load and refuses to play them; the hub is the
 *  authority and validates with the same rules. */
export function validateAnimation(anim) {
  const errs = [];
  if (!anim || typeof anim !== 'object' || Array.isArray(anim)) {
    return ['animation must be an object'];
  }
  if (typeof anim.name !== 'string' || !NAME_RE.test(anim.name)) {
    errs.push(`name must match ${NAME_RE}`);
  }
  const dur = anim.duration_s;
  if (typeof dur !== 'number' || !Number.isFinite(dur) || dur < 0.4 || dur > 8) {
    errs.push('duration_s must be 0.4..8');
  }
  if (anim.loop !== undefined && typeof anim.loop !== 'boolean') {
    errs.push('loop must be a boolean when present');
  }
  if (anim.ease !== undefined && anim.ease !== 'smooth' && anim.ease !== 'linear') {
    errs.push("ease must be 'smooth' or 'linear' when present");
  }
  const keys = anim.keyframes;
  if (!Array.isArray(keys) || keys.length < 2 || keys.length > 24) {
    errs.push('keyframes must hold 2..24 entries');
    return errs;
  }
  let prev = -1;
  keys.forEach((k, i) => {
    const at = `keyframes[${i}]`;
    if (!k || typeof k !== 'object' || Array.isArray(k)) {
      errs.push(`${at} must be an object`); return;
    }
    if (typeof k.t !== 'number' || !Number.isFinite(k.t) || k.t <= prev) {
      errs.push(`${at}.t must be finite and strictly ascending`);
    } else prev = k.t;
    if (k.bones !== undefined) {
      if (!k.bones || typeof k.bones !== 'object' || Array.isArray(k.bones)) {
        errs.push(`${at}.bones must be an object when present`);
      } else {
        for (const [bone, v] of Object.entries(k.bones)) {
          if (bone === 'neck') { errs.push(`${at}: neck is never allowed`); continue; }
          if (!Object.prototype.hasOwnProperty.call(KEYFRAME_LIMITS, bone)) {
            errs.push(`${at}: unknown bone ${bone}`); continue;
          }
          if (!isVec3(v)) errs.push(`${at}: ${bone} must be [x,y,z] finite degrees`);
        }
      }
    }
    if (k.hips_y !== undefined &&
        (typeof k.hips_y !== 'number' || !Number.isFinite(k.hips_y))) {
      errs.push(`${at}.hips_y must be a finite number when present`);
    }
    if (k.expression !== undefined) {
      if (!k.expression || typeof k.expression !== 'object' || Array.isArray(k.expression)) {
        errs.push(`${at}.expression must be an object when present`);
      } else {
        for (const [name, w] of Object.entries(k.expression)) {
          if (!KEYFRAME_EXPRESSIONS.includes(name)) {
            errs.push(`${at}: unknown expression ${name}`);
          } else if (typeof w !== 'number' || !Number.isFinite(w) || w < 0 || w > 1) {
            errs.push(`${at}: expression ${name} must be 0..1`);
          }
        }
      }
    }
  });
  if (errs.length === 0) {
    if (keys[0].t !== 0) errs.push('first keyframe t must be 0');
    if (keys[keys.length - 1].t !== dur) errs.push('last keyframe t must equal duration_s');
  }
  return errs;
}

/** Defense in depth on playback (docs/SELF_ANIMATION.md): clamp again here.
 *  Returns {anim, changes: [string]} — `changes` lists what was rewritten. */
export function clampAnimation(anim) {
  const changes = [];
  const keys = (anim.keyframes || []).map((k, i) => {
    const nk = { ...k };
    if (nk.bones) {
      const bones = {};
      for (const [bone, v] of Object.entries(nk.bones)) {
        const lim = KEYFRAME_LIMITS[bone];
        if (!lim || !isVec3(v)) {          // neck/unknown/garbage: drop it
          changes.push(`keyframes[${i}].${bone}: dropped`);
          continue;
        }
        const out = v.map((deg, axis) => {
          const [lo, hi] = lim[['x', 'y', 'z'][axis]];
          if (deg < lo || deg > hi) {
            changes.push(`keyframes[${i}].${bone}.${['x', 'y', 'z'][axis]} ` +
                         `${deg}° -> ${clamp(deg, lo, hi)}°`);
            return clamp(deg, lo, hi);
          }
          return deg;
        });
        bones[bone] = out;
      }
      nk.bones = bones;
    }
    if (typeof nk.hips_y === 'number' && Number.isFinite(nk.hips_y)) {
      const c = clamp(nk.hips_y, HIPS_Y_MIN, HIPS_Y_MAX);
      if (c !== nk.hips_y) {
        changes.push(`keyframes[${i}].hips_y ${nk.hips_y} -> ${c}`);
        nk.hips_y = c;
      }
    }
    if (nk.expression) {
      const ex = {};
      for (const [name, w] of Object.entries(nk.expression)) {
        if (!KEYFRAME_EXPRESSIONS.includes(name)) {
          changes.push(`keyframes[${i}].expression.${name}: dropped`);
          continue;
        }
        ex[name] = clamp(typeof w === 'number' && Number.isFinite(w) ? w : 0, 0, 1);
      }
      nk.expression = ex;
    }
    return nk;
  });
  return { anim: { ...anim, keyframes: keys }, changes };
}

// ── sampling ─────────────────────────────────────────────────

/**
 * Pure frame sample. `t` is seconds from the animation's start (the pose
 * layer advances it at frame rate). Looped animations wrap t inside
 * duration_s; non-looped clamp to [0, duration_s] — the caller decides when
 * a finished one-shot blends out.
 *
 * -> { bones: {<bone>: {x,y,z,w}},   // local rest-relative quaternions
 *      expression: {<preset>: 0..1}, // interpolated weights
 *      hips_y: number }              // metres, interpolated
 * Bones that are identity at BOTH neighbouring keys are omitted (the pose
 * layer leaves unkeyed bones exactly alone: feet stay planted unless a leg
 * bone is keyed).
 */
export function sample(anim, t) {
  const keys = (anim && anim.keyframes) || [];
  const out = { bones: {}, expression: {}, hips_y: 0 };
  if (keys.length === 0) return out;
  const dur = Number(anim.duration_s) || keys[keys.length - 1].t || 0.001;
  let tt = Number.isFinite(t) ? t : 0;
  if (anim.loop && dur > 0) tt = ((tt % dur) + dur) % dur;
  else tt = clamp(tt, 0, dur);

  let i = 0;
  while (i < keys.length - 2 && keys[i + 1].t <= tt) i++;
  const a = keys[i], b = keys[Math.min(i + 1, keys.length - 1)];
  const span = b.t - a.t;
  let u = span > 0 ? clamp((tt - a.t) / span, 0, 1) : 1;
  if (anim.ease !== 'linear') u = easeSmooth(u);

  const boneNames = new Set([
    ...Object.keys(a.bones || {}), ...Object.keys(b.bones || {}),
  ]);
  for (const bone of boneNames) {
    if (bone === 'neck') continue;                    // belt and braces
    const lim = KEYFRAME_LIMITS[bone];
    if (!lim) continue;                               // unknown bone: rest
    const va = clampVec(a.bones && a.bones[bone], lim);
    const vb = clampVec(b.bones && b.bones[bone], lim);
    const qa = eulerToQuatXYZ(...va);
    const qb = eulerToQuatXYZ(...vb);
    if (quatIsIdentity(qa) && quatIsIdentity(qb)) continue;
    const q = quatSlerp(qa, qb, u);
    if (quatIsIdentity(q)) continue;   // passing exactly through rest
    out.bones[bone] = q;
  }
  const ea = a.expression || {}, eb = b.expression || {};
  for (const name of new Set([...Object.keys(ea), ...Object.keys(eb)])) {
    if (!KEYFRAME_EXPRESSIONS.includes(name)) continue;
    const wa = typeof ea[name] === 'number' ? ea[name] : 0;
    const wb = typeof eb[name] === 'number' ? eb[name] : 0;
    out.expression[name] = clamp(wa + (wb - wa) * u, 0, 1);
  }
  const ha = Number.isFinite(a.hips_y) ? a.hips_y : 0;
  const hb = Number.isFinite(b.hips_y) ? b.hips_y : 0;
  out.hips_y = clamp(ha + (hb - ha) * u, HIPS_Y_MIN, HIPS_Y_MAX);
  return out;
}

function clampVec(v, lim) {
  if (!isVec3(v)) return [0, 0, 0];
  return [
    clamp(v[0], lim.x[0], lim.x[1]),
    clamp(v[1], lim.y[0], lim.y[1]),
    clamp(v[2], lim.z[0], lim.z[1]),
  ];
}
