// Proportion-aware arm math for gestures (pure: no three.js; stdlib-only ESM
// that runs unchanged in the browser, in `node --test`, and in any harness).
//
// Why this exists: the hand-tuned Euler keyframes in gestures.js encode the
// *sample avatar's* bone local axes and limb lengths. A different VRM — the
// owner's VRM 0.x 'Furina' model has notably longer arms, no chest bone, and
// VRM0 bone axes — lands those same numbers somewhere else entirely (arm
// not raised at all, forearm folded into the head, wrist below the chin).
// For wave/think we instead define where the HAND should end up in world
// space, relative to this rig's own head/chin/shoulders, and solve the
// shoulder->elbow->wrist chain with a 2-bone analytic IK. That works on any
// humanoid rig, VRM 0.x or 1.0, because it uses only joint positions
// measured from the rig — never a bone-axis convention.
//
// Conventions
// ───────────
// Points are plain [x, y, z] arrays in *world* space. After
// VRMUtils.rotateVRM0() the character faces +Z and her right side is -X.
// The "rig profile" (built by motion.js at attach()) carries measurements:
//
//   rig = {
//     arms: { right: { sideSign, shoulder:[x,y,z], elbow:[x,y,z],
//                      wrist:[x,y,z],            // REST pose
//                      l1, l2, len }, left: {...} },
//     shoulderWidth, headLen, headTop, head:[x,y,z], chin:[x,y,z],
//     chest:[x,y,z], bboxTop, bboxBottom,
//   }
//
// Arm keys live on gestures (g.arms = { right: { keys, osc }, left: ... }):
//   { t, target?: {ref, unit, off, abs}, pole?: [x,y,z], hand?: [x,y,z] }
// A key without `target` is the rest pose, so one-shots ease the arm up from
// and back down to rest exactly like the Euler keyframes do. Target specs put
// the wrist relative to an anchor, in rig-relative units so the gesture
// scales with any body:
//   ref:  'headTop' | 'chin' | 'shoulder' | 'chest' | 'otherElbow'
//   unit: 'arm' | 'head' | 'w'        // multiplier for `off`
//   off:  [x, y, z]                   // x is mirrored by sideSign (right=-x)
// A pole vector is the world direction the elbow bends toward.

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smoothstep = (u) => u * u * (3 - 2 * u);

export const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
export const mul = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
export const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const len = (a) => Math.hypot(a[0], a[1], a[2]);
export const dist = (a, b) => len(sub(a, b));
export const norm = (a) => {
  const l = len(a);
  return l > 1e-9 ? [a[0] / l, a[1] / l, a[2] / l] : [0, 0, 0];
};
const anyPerp = (a) => {
  const ax = Math.abs(a[0]), ay = Math.abs(a[1]), az = Math.abs(a[2]);
  const base = ax <= ay && ax <= az ? [1, 0, 0] : ay <= az ? [0, 1, 0] : [0, 0, 1];
  return norm(sub(base, mul(a, dot(base, a))));
};

/**
 * Classic 2-bone analytic IK: shoulder S fixed, find the elbow so the chain
 * reaches the wrist target (clamped to the reachable shell), elbow bent
 * toward `pole`. Returns { elbow, wrist, dirUpper, dirLower } or null when
 * the chain is degenerate.
 */
export function solveTwoBoneIK(S, T, l1, l2, pole) {
  if (!(l1 > 1e-6 && l2 > 1e-6)) return null;
  let d = sub(T, S);
  let dLen = len(d);
  if (dLen < 1e-6) { d = [0, -1, 0]; dLen = 1e-6; }
  const dir = mul(d, 1 / dLen);
  const maxR = (l1 + l2) * 0.999;
  const minR = Math.abs(l1 - l2) + (l1 + l2) * 0.002;
  const reach = clamp(dLen, minR, maxR);
  const wrist = add(S, mul(dir, reach));
  const a = (l1 * l1 - l2 * l2 + reach * reach) / (2 * reach);
  const h = Math.sqrt(Math.max(0, l1 * l1 - a * a));
  let p = norm(pole || [0, 0, -1]);
  if (len(p) < 1e-6) p = anyPerp(dir);
  let n = sub(p, mul(dir, dot(p, dir)));
  n = len(n) < 1e-6 ? anyPerp(dir) : norm(n);
  const elbow = add(add(S, mul(dir, a)), mul(n, h));
  return {
    elbow, wrist,
    dirUpper: norm(sub(elbow, S)),
    dirLower: norm(sub(wrist, elbow)),
  };
}

/** Resolve one target spec against the rig (+ runtime ctx: otherElbow). */
export function resolveTarget(spec, rig, ctx = {}) {
  const side = ctx.side || 'right';
  const arm = rig.arms[side];
  const other = rig.arms[side === 'right' ? 'left' : 'right'];
  let base;
  switch (spec.ref) {
    case 'chin': base = rig.chin; break;
    case 'headTop': base = [rig.head[0], rig.headTop, rig.head[2]]; break;
    case 'chest': base = rig.chest; break;
    case 'otherElbow': base = ctx.otherElbow || (other ? other.elbow : rig.chest); break;
    case 'shoulder':
    default: base = arm ? arm.shoulder : rig.chest; break;
  }
  const unit = spec.unit === 'head' ? rig.headLen
    : spec.unit === 'w' ? rig.shoulderWidth
    : arm ? arm.len : rig.headLen;
  const s = ctx.sideSign ?? (arm ? arm.sideSign : 1);
  const off = spec.off || [0, 0, 0];
  const p = add(base, [s * off[0] * unit, off[1] * unit, off[2] * unit]);
  return spec.abs ? add(p, spec.abs) : p;
}

const lerp3 = (a, b, u) => [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u];

/**
 * Sample one side's IK arm track at time t: interpolate the resolved world
 * wrist target / pole / hand rotation between keys (smoothstep-eased), fold
 * in target-space oscillators (the wave's arm rock; amp is a fraction of the
 * oscillator unit, eased in/out at the window edges).
 * Returns { w, target:[x,y,z]|null, pole, hand } with w = pose weight
 * (0 = rest). `ctx` carries 'otherElbow' for cross-arm refs.
 */
export function sampleArm(side, track, t, rig, { loop = false, duration = Infinity, ctx = {} } = {}) {
  if (!track || !track.keys || !track.keys.length) return null;
  const keys = track.keys;
  const arm = rig.arms[side];
  const sctx = { side, sideSign: arm ? arm.sideSign : 1, ...ctx };
  const timed = keys.map((k) => ({ ...k, target: k.target ? resolveTarget(k.target, rig, sctx) : null }));
  let time = loop ? ((t % duration) + duration) % duration : clamp(t, 0, duration);
  let a = timed[0], b = timed[timed.length - 1];
  for (let i = 0; i < timed.length - 1; i++) {
    if (time >= timed[i].t && time <= timed[i + 1].t) { a = timed[i]; b = timed[i + 1]; break; }
  }
  const span = b.t - a.t;
  const u = span > 0 ? smoothstep((time - a.t) / span) : 1;
  const wA = a.target ? 1 : 0, wB = b.target ? 1 : 0;
  const w = wA + (wB - wA) * u;
  if (w < 1e-4) return { w: 0, target: null, pole: null, hand: null };
  const shape = a.target || b.target;
  const target = lerp3(a.target || shape, b.target || shape, u);
  const pole = lerp3(norm(a.pole || b.pole), norm(b.pole || a.pole), u);
  const hand = (a.hand || b.hand) ? lerp3(a.hand || b.hand, b.hand || a.hand, u) : null;
  const oscs = track.osc ? (Array.isArray(track.osc) ? track.osc : [track.osc]) : [];
  for (const o of oscs) {
    if (t < o.from || t > o.until) continue;
    const edge = clamp(Math.min(t - o.from, o.until - t) / 0.25, 0, 1);
    const wob = Math.sin((t - o.from) * o.freq * Math.PI * 2) * o.amp * smoothstep(edge);
    target[o.axis % 3] += wob * (o.unit === 'w' ? rig.shoulderWidth : arm ? arm.len : rig.headLen);
  }
  return { w, target, pole, hand };
}

/**
 * The world-space box the IK gestures sweep (wave is the widest), computed by
 * numerically sampling the gesture arm tracks against this rig. Used for
 * gesture-aware camera framing in index.html: nothing inside the box may
 * leave the canvas at the default window size. handR approximates the hand's
 * own size so the *fingertips*, not just the wrist bone, fit inside.
 */
export function gestureEnvelope(rig, armsByName, durations) {
  const arm = rig.arms.right || rig.arms.left;
  const L = arm ? arm.len : 0.5;
  const handR = Math.max(0.45 * rig.headLen, 0.35 * L);
  let yTop = rig.headTop + handR;
  let xHalf = rig.shoulderWidth / 2 + handR;
  for (const [name, g] of Object.entries(armsByName)) {
    const tracks = g.arms || g;
    const dur = durations[name] || g.duration || 3.5;
    for (const side of ['right', 'left']) {
      const track = tracks[side];
      if (!track) continue;
      for (let t = 0; t <= dur; t += 0.05) {
        const s = sampleArm(side, track, t, rig, { loop: !!g.loop, duration: dur });
        if (!s || !s.target || s.w < 0.5) continue;
        yTop = Math.max(yTop, s.target[1] + handR);
        xHalf = Math.max(xHalf, Math.abs(s.target[0]) + handR);
      }
    }
  }
  return { yTop, xHalf, yBottom: Math.min(rig.bboxBottom, 0) };
}
