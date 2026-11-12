// node --test client/js/legs.js — the hips_y hop-leg solver (pure, no three).
// Synthetic straight-ish legs: hip (0.09, 0.90, 0) -> knee (0.09, 0.48, 0.012)
// -> ankle (0.09, 0.06, -0.005): l1 = 0.4204, l2 = 0.4206, facing +Z.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { solveHopLeg, solveHopLegs, rotAxis, hopFrame, HOP_LEGS } from './legs.js';
import { sub, len, dist, norm, dot } from './ik.js';

const REST = {
  hip: [0.09, 0.90, 0],
  knee: [0.09, 0.48, 0.012],
  ankle: [0.09, 0.06, -0.005],
};
const F = [0, 0, 1];
const solve = (hipsY, over = {}) => solveHopLeg({ ...REST, forward: F, hipsY, ...over });
const deg = Math.PI / 180;

test('planted: hips_y -0.05 / -0.10 keep the foot on its rest world spot', () => {
  for (const y of [-0.05, -0.10]) {
    const s = solve(y);
    assert.equal(s.mode, 'planted', `y=${y}`);
    assert.ok(dist(s.anklePos, REST.ankle) <= 0.01,
      `y=${y}: ankle drifted ${dist(s.anklePos, REST.ankle).toFixed(4)} m`);
    const drift = Math.hypot(s.anklePos[0] - REST.ankle[0], s.anklePos[2] - REST.ankle[2]);
    assert.ok(drift <= 0.02, `y=${y}: xz drift ${drift.toFixed(4)} m`);
    // bone lengths are preserved (rig truth, not assumed numbers)
    const H = [REST.hip[0], REST.hip[1] + y, REST.hip[2]];
    assert.ok(Math.abs(len(sub(s.kneePos, H)) - dist(REST.hip, REST.knee)) < 1e-9);
    assert.ok(Math.abs(dist(s.anklePos, s.kneePos) - dist(REST.knee, REST.ankle)) < 1e-6);
  }
});

test('planted: knees bend FORWARD (pole = facing), chain lengths preserved', () => {
  const s = solve(-0.08);
  assert.ok(s.thighDir[2] > 0.05, `thigh must swing forward (+z), got ${s.thighDir[2]}`);
  assert.ok(s.kneePos[2] > REST.knee[2], 'knee bulges toward facing');
  assert.ok(s.kneePos[1] < REST.hip[1] - 0.05, 'knee stays below the (sunk) hip');
  assert.ok(Math.abs(s.kneeFlexDeg - 50) < 15, `crouch knee flex ${s.kneeFlexDeg} far from expected ~55`);
  // hip sink is exactly the requested hips_y
  const H = [REST.hip[0], REST.hip[1] - 0.08, REST.hip[2]];
  assert.ok(Math.abs(len(sub(s.kneePos, H)) - dist(REST.hip, REST.knee)) < 1e-9);
  assert.ok(Math.abs(dist(s.anklePos, s.kneePos) - dist(REST.knee, REST.ankle)) < 1e-6);
});

test('planted: hips sink without moving the foot at all depths', () => {
  for (const y of [-0.02, -0.08, -0.15]) {
    const s = solve(y);
    const kneeDrop = REST.hip[1] + y + 0 - (s.kneePos[1]);
    assert.ok(Number.isFinite(kneeDrop));
    assert.ok(dist(s.anklePos, REST.ankle) <= 0.01, `y=${y} foot moved`);
  }
});

test('planted limits: a dip deeper than the knee can fold SINKS the foot', () => {
  // d(hip->ankle) ~0.84 m, l1=l2~0.42: a 0.60 m dip asks for >126 deg flex
  const s = solve(-0.60);
  assert.ok(s.kneeFlexDeg <= HOP_LEGS.kneeMaxDeg + 0.5,
    `knee over-rotated: ${s.kneeFlexDeg} > ${HOP_LEGS.kneeMaxDeg}`);
  assert.ok(s.anklePos[1] < REST.ankle[1] - 0.01,
    `foot should sink below rest, got dy=${(s.anklePos[1] - REST.ankle[1]).toFixed(3)}`);
  // still no sideways nonsense
  assert.ok(Math.abs(s.anklePos[0] - REST.ankle[0]) < 0.05);
});

test('tuck: 95 deg knee at hips_y>=0.10, thigh=0.3*knee, toe 15 deg beyond shin', () => {
  const s = solve(0.12);
  assert.equal(s.mode, 'tuck');
  assert.ok(Math.abs(s.kneeFlexDeg - 95) < 5, `knee flex ${s.kneeFlexDeg} not ~95`);
  assert.ok(Math.abs(s.thighFlexDeg - 28.5) < 4, `thigh flex ${s.thighFlexDeg} not ~0.3*knee`);
  // foot rides with the shin (shin swung BACK by thigh-knee) and points 15 deg further
  const shinDeg = (s.thighAngle - 95 * deg) / deg;
  assert.ok(shinDeg < -60 && shinDeg > -72, `shin swing ${shinDeg}`);
  const pointDeg = (s.footArc.angle - s.shinAngle) / deg;
  assert.ok(Math.abs(pointDeg + 15) < 0.01, `foot point ${pointDeg} deg (want -15)`);
  // hips (and the whole chain) ride UP with the body: feet clearly airborne
  assert.ok(s.anklePos[1] > REST.ankle[1] + 0.15, 'feet must rise well off the ground');
  // hinge axis: rotating DOWN about it swings FORWARD (+z) for facing +Z
  const swung = rotAxis([0, -1, 0], s.footArc.axis, 0.3);
  assert.ok(swung[2] > 0.2, 'sagittal axis sign wrong');
});

test('tuck: flex rises smoothly and caps at fullY', () => {
  const flex = (y) => solve(y).kneeFlexDeg;
  const a = flex(0.02), b = flex(0.05), c = flex(0.10);
  assert.ok(a < b && b < c, `not monotonic: ${a} ${b} ${c}`);
  assert.ok(flex(0.15) - c < 1 && flex(0.5) - c < 1, 'tuck must cap at hips_y=0.10');
  // smooth in: quarter height is well under half flex (smoothstep, not linear)
  assert.ok(flex(0.05) < 0.55 * HOP_LEGS.kneeDeg, `ease-in too steep: ${flex(0.05)}`);
});

test('continuity at hips_y = 0: both branches == exact rest', () => {
  const s = solve(0);
  assert.equal(s.mode, 'tuck');
  assert.ok(dist(s.kneePos, REST.knee) < 1e-9, `knee moved ${dist(s.kneePos, REST.knee)}`);
  assert.ok(dist(s.anklePos, REST.ankle) < 1e-9, `ankle moved ${dist(s.anklePos, REST.ankle)}`);
  assert.ok(Math.abs(s.footArc.angle) < 1e-9);
  // and NO pop across zero from either side (mm-scale agreement)
  const l = solve(-1e-4), r = solve(1e-4);
  assert.ok(dist(l.anklePos, REST.ankle) < 2e-3);
  assert.ok(dist(r.anklePos, REST.ankle) < 2e-3);
  for (const s2 of [l, r]) {
    assert.ok(len(sub(s2.thighDir, norm(sub(REST.knee, REST.hip)))) < 0.02);
  }
});

test('asymmetry: right leg flexes ~10 % less than the left at full tuck', () => {
  const r = solveHopLegs({ legs: { left: REST, right: REST }, forward: F, hipsY: 0.12 });
  assert.ok(r.left && r.right);
  const ratio = r.right.kneeFlexDeg / r.left.kneeFlexDeg;
  assert.ok(ratio > 0.85 && ratio < 0.95, `L/R flex ratio ${ratio.toFixed(3)} not ~0.9`);
});

test('solveHopLegs null-guards missing / degenerate legs', () => {
  const r = solveHopLegs({ legs: { left: REST, right: undefined }, forward: F, hipsY: 0.1 });
  assert.ok(r.left);
  assert.equal(r.right, null);
  const r2 = solveHopLegs({ legs: null, forward: F, hipsY: -0.1 });
  assert.equal(r2.left, null);
  const folded = { hip: [0, 0.9, 0], knee: [0, 0.899, 0], ankle: [0, 0.5, 0] };  // l1 degenerate
  assert.equal(solveHopLeg({ ...folded, forward: F, hipsY: -0.1 }), null);
});

test('bent-rest rig keeps its own knee direction (pole from rest, not assumed)', () => {
  // knee pushed BACK at rest (-z): the solver must keep bending that way
  const bent = { hip: [0, 0.9, 0], knee: [0, 0.5, -0.06], ankle: [0, 0.06, 0] };
  const s = solveHopLeg({ ...bent, forward: F, hipsY: 0 });
  assert.ok(dist(s.kneePos, bent.knee) < 1e-9, 'continuity at 0 broken for bent rest');
  const c = solveHopLeg({ ...bent, forward: F, hipsY: -0.08 });
  assert.ok(c.kneePos[2] < bent.knee[2] + 1e-6, 'crouch pushed the knee the WRONG way');
  assert.ok(c.kneeFlexDeg > s.kneeFlexDeg + 10, 'crouch did not deepen the bend');
});

test('hopFrame: hinge axis sign holds for any facing (rotated character)', () => {
  for (const yaw of [0, 0.7, 2.2, -1.1]) {
    const f = [Math.sin(yaw), 0, Math.cos(yaw)];
    const { axis, forward } = hopFrame(f);
    assert.ok(Math.abs(dot(axis, forward)) < 1e-9);
    assert.ok(Math.abs(dot(axis, [0, 1, 0])) < 1e-9);
    const swung = rotAxis([0, -1, 0], axis, 0.5);
    assert.ok(dot(swung, forward) > 0.4, 'positive rotation must swing the leg forward');
  }
});

test('planted xz drift stays <= 2 cm for the whole contract dip range', () => {
  for (let y = -0.01; y >= -0.25; y -= 0.01) {
    const s = solve(y);
    const drift = Math.hypot(s.anklePos[0] - REST.ankle[0], s.anklePos[2] - REST.ankle[2]);
    const dy = Math.abs(s.anklePos[1] - REST.ankle[1]);
    assert.ok(drift <= 0.02 && dy <= 0.01,
      `y=${y.toFixed(2)}: drift ${drift.toFixed(4)} dy ${dy.toFixed(4)}`);
    assert.ok(s.kneeFlexDeg >= 0 && s.kneeFlexDeg <= HOP_LEGS.kneeMaxDeg + 0.5);
  }
});
