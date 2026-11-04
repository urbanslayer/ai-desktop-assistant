// node --test client/js/ik.test.js
//
// The gesture IK/retarget math against TWO synthetic rigs with very
// different proportions: `compact` (short arms, small head, like a chibi
// model) and `elongated` (long arms + big hat clearance, like the owner's
// VRM 0.x 'Furina' model). Every gesture-target acceptance number is
// expressed RELATIVE to each rig, so a retarget that only works on one body
// fails here even if it measures fine on the sample avatar.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { solveTwoBoneIK, resolveTarget, sampleArm, gestureEnvelope, dist } from './ik.js';
import { GESTURES, IK_ARM_GESTURES } from './gestures.js';

// ── synthetic rig profiles (same shape motion.js measures from real VRMs) ──
function makeRig(kind) {
  const E = kind === 'elongated';
  const head = [0, E ? 1.52 : 1.30, 0];
  const headLen = E ? 0.22 : 0.17;
  const l1 = E ? 0.34 : 0.22, l2 = E ? 0.30 : 0.20;
  const shX = E ? 0.165 : 0.14;
  const shY = E ? 1.42 : 1.20;
  const mk = (side, sign) => {
    const shoulder = [sign * shX, shY, 0];
    // rest chain hangs down and slightly out (arms-lowered rest)
    const elbow = [shoulder[0] + sign * 0.06, shoulder[1] - l1 * 0.97, 0];
    const wrist = [elbow[0] + sign * 0.02, elbow[1] - l2 * 0.99, 0.01];
    const nrm = (a, b) => {
      const v = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
      const l = Math.hypot(...v);
      return v.map((x) => x / l);
    };
    return { side, sideSign: sign, shoulder, elbow, wrist, l1, l2, len: l1 + l2,
             restDirUpper: nrm(shoulder, elbow), restDirLower: nrm(elbow, wrist) };
  };
  return {
    arms: { right: mk('right', -1), left: mk('left', 1) },
    shoulderWidth: 2 * shX,
    headLen,
    headTop: head[1] + 0.62 * headLen,
    head, neck: [0, head[1] - headLen * 0.8, 0],
    chin: [head[0], head[1] - 0.5 * headLen, head[2] + 0.35 * headLen],
    chest: [0, shY - 0.15, 0],
    bboxTop: E ? head[1] + 0.9 * headLen + 0.25 : head[1] + 0.7 * headLen, // the hat
    bboxBottom: 0,
  };
}
const RIGS = { compact: makeRig('compact'), elongated: makeRig('elongated') };

// ── the 2-bone solver itself ─────────────────────────────────────────
test('solveTwoBoneIK hits the target and keeps bone lengths exact', () => {
  const S = [0, 1.4, 0];
  const T = [-0.3, 1.7, 0.15];  // comfortably reachable for l1=.3 l2=.28
  const r = solveTwoBoneIK(S, T, 0.3, 0.28, [-0.5, -0.7, -0.3]);
  assert.ok(r);
  assert.ok(dist(r.wrist, T) < 1e-9, `wrist must land on target, off by ${dist(r.wrist, T)}`);
  assert.ok(Math.abs(dist(S, r.elbow) - 0.3) < 1e-9, 'upper arm length drifted');
  assert.ok(Math.abs(dist(r.elbow, r.wrist) - 0.28) < 1e-9, 'forearm length drifted');
});

test('solveTwoBoneIK clamps unreachable targets to the shell', () => {
  const S = [0, 1.4, 0];
  const far = solveTwoBoneIK(S, [2, 1.4, 0], 0.3, 0.28, [0, -1, 0]);
  assert.ok(Math.abs(dist(S, far.wrist) - 0.58 * 0.999) < 1e-6, 'too-far target must clamp to reach');
  const near = solveTwoBoneIK(S, [0, 1.4001, 0], 0.3, 0.28, [0, 0, -1]);
  assert.ok(dist(S, near.wrist) > 0.02, 'too-close target must clamp to the inner shell');
  assert.ok(Math.abs(dist(S, near.elbow) - 0.3) < 1e-9);
  assert.ok(Math.abs(dist(near.elbow, near.wrist) - 0.28) < 1e-9);
});

test('solveTwoBoneIK bends the elbow toward the pole', () => {
  const S = [0, 1.4, 0], T = [-0.25, 1.65, 0.05];
  const down = solveTwoBoneIK(S, T, 0.3, 0.28, [0, -1, 0]);
  const up = solveTwoBoneIK(S, T, 0.3, 0.28, [0, 1, 0]);
  // project elbow onto the axis perpendicular to S->T: pole side wins
  const yDown = down.elbow[1], yUp = up.elbow[1];
  assert.ok(yDown < yUp, `pole must flip the elbow (down ${yDown} vs up ${yUp})`);
});

test('solveTwoBoneIK is degenerate-safe', () => {
  assert.equal(solveTwoBoneIK([0, 1, 0], [0, 1.2, 0], 0, 0.28, [0, -1, 0]), null);
  const r = solveTwoBoneIK([0, 1, 0], [0, 1, 0], 0.3, 0.28, [0, 0, 0]);
  assert.ok(r && Number.isFinite(r.elbow[0]), 'zero-length target must still solve');
});

// ── proportion-aware retargeting: same gesture, any body ────────────
for (const [name, rig] of Object.entries(RIGS)) {
  test(`wave wrist target: same relative spot on the ${name} rig`, () => {
    const arm = rig.arms.right;
    const s = sampleArm('right', GESTURES.wave.arms.right, 1.0, rig,
      { duration: GESTURES.wave.duration });
    assert.ok(s && s.w > 0.99 && s.target);
    // inside the reachable shell (with a little margin for the clamp)
    const reach = dist(arm.shoulder, s.target);
    assert.ok(reach <= arm.len * 0.98, `target ${reach.toFixed(3)} m out of arm ${arm.len} m`);
    assert.ok(reach >= Math.abs(arm.l1 - arm.l2) + 1e-6, 'target collapsed on the shoulder');
    // hand above the shoulder by > 0.25 arm lengths (acceptance, relative)
    assert.ok(s.target[1] - arm.shoulder[1] > 0.25 * arm.len,
      `wave hand must clear the shoulder: ${s.target[1] - arm.shoulder[1]}`);
    // and at head-top height (the owner's "hand near the head" wave)
    assert.ok(s.target[1] >= rig.headTop - 0.05, 'wave hand must reach the head top');
    // slightly in front of the chest plane, never behind the body
    assert.ok(s.target[2] >= rig.chest[2], 'wave hand must stay in front of the chest');
    // start and end at rest (one-shot easing without the blend layer)
    assert.equal(sampleArm('right', GESTURES.wave.arms.right, 0.0, rig, { duration: 2.4 }).w, 0);
    assert.equal(sampleArm('right', GESTURES.wave.arms.right, 2.4, rig, { duration: 2.4 }).w, 0);
  });

  test(`wave arm-rock swings > 0.15 forearm lengths on the ${name} rig`, () => {
    const arm = rig.arms.right;
    let min = Infinity, max = -Infinity;
    for (let t = 0.5; t <= 2.05; t += 0.02) {
      const s = sampleArm('right', GESTURES.wave.arms.right, t, rig, { duration: 2.4 });
      min = Math.min(min, s.target[0]); max = Math.max(max, s.target[0]);
    }
    const swing = max - min;
    assert.ok(swing > 0.15 * arm.l2, `lateral swing ${swing} vs forearm ${arm.l2}`);
    // and the whole swing stays reachable (|min| measured from the centre
    // line; the shoulder sits |shoulder.x| off it)
    assert.ok(Math.abs(min) <= arm.len + Math.abs(arm.shoulder[0]) + 1e-6);
  });

  test(`think: wrist lands on the chin and the left hand cups the elbow on the ${name} rig`, () => {
    const spec = GESTURES.think.arms;
    const r = sampleArm('right', spec.right, 1.0, rig, { loop: true, duration: 4.0 });
    assert.ok(r && r.w > 0.99);
    // acceptance: handToChin < 0.07 m scaled by head size (0.12 m headLen
    // reference — the sample avatar), wrist-braced so curled fingers touch
    const dChin = dist(r.target, rig.chin);
    assert.ok(dChin < 0.07 * (rig.headLen / 0.12) * 1.05, `handToChin ${dChin}`);
    // the solved right wrist must be reachable
    const armR = rig.arms.right;
    assert.ok(dist(armR.shoulder, r.target) <= armR.len * 0.98, 'chin target out of reach');
    // the left hand resolves against the SOLVED right elbow (the thinker tripod)
    const sol = solveTwoBoneIK(armR.shoulder, r.target, armR.l1, armR.l2, r.pole);
    const l = sampleArm('left', spec.left, 1.0, rig,
      { loop: true, duration: 4.0, ctx: { otherElbow: sol.elbow } });
    assert.ok(l && l.w > 0.99);
    const armL = rig.arms.left;
    assert.ok(dist(armL.shoulder, l.target) <= armL.len * 0.98, 'left support target out of reach');
    const elbowDist = dist(l.target, sol.elbow);
    assert.ok(elbowDist < 0.6 * rig.headLen, `left hand must cup the elbow, ${elbowDist}`);
  });

  test(`gesture envelope contains the wave sweep on the ${name} rig`, () => {
    const durs = Object.fromEntries(Object.entries(IK_ARM_GESTURES).map(([n, g]) => [n, g.duration]));
    const env = gestureEnvelope(rig, IK_ARM_GESTURES, durs);
    const arm = rig.arms.right;
    for (let t = 0; t <= 2.4; t += 0.05) {
      const s = sampleArm('right', GESTURES.wave.arms.right, t, rig, { duration: 2.4 });
      if (!s.target || s.w < 0.5) continue;
      const handR = Math.max(0.45 * rig.headLen, 0.35 * arm.len);
      assert.ok(env.yTop >= s.target[1] + handR * 0.99, `envelope top ${env.yTop} < ${s.target[1]}`);
      assert.ok(env.xHalf >= Math.abs(s.target[0]) + handR * 0.99, `envelope wide enough for ${s.target[0]}`);
    }
    assert.ok(env.yTop > rig.bboxTop, 'framing must also clear the hat/top of head');
    assert.ok(env.yBottom <= 0);
  });
}

// ── target-spec plumbing ─────────────────────────────────────────────
test('resolveTarget mirrors x by side and scales by the chosen unit', () => {
  const rig = RIGS.compact;
  const spec = { ref: 'headTop', unit: 'arm', off: [0.6, 0.1, 0.2] };
  const r = resolveTarget(spec, rig, { side: 'right' });
  const l = resolveTarget(spec, rig, { side: 'left' });
  assert.ok(r[0] < 0 && l[0] > 0, 'x must flip with the side');
  assert.ok(Math.abs(r[0] + l[0]) < 1e-12);
  assert.ok(Math.abs(r[1] - (rig.headTop + 0.1 * rig.arms.right.len)) < 1e-12);
  const wSpec = { ref: 'chest', unit: 'w', off: [1, 0, 0] };
  const w = resolveTarget(wSpec, rig, { side: 'left' });
  assert.ok(Math.abs(w[0] - rig.shoulderWidth) < 1e-12, 'w unit must be the shoulder width');
});
