// node --test client/js/legs.js — the hips_y hop-leg solver (pure, no three).
// Synthetic straight-ish legs: hip (0.09, 0.90, 0) -> knee (0.09, 0.48, 0.012)
// -> ankle (0.09, 0.06, -0.005): l1 = 0.4204, l2 = 0.4206, facing +Z.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { solveHopLeg, solveHopLegs, rotAxis, hopFrame, HOP_LEGS,
         landingTouchdowns, landingCushion } from './legs.js';
import { sample as sampleDoc } from './keyframes.js';
import { sub, len, dist, norm, dot, mul } from './ik.js';

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
  assert.ok(Math.abs(s.kneeFlexDeg - HOP_LEGS.kneeDeg) < 7, `knee flex ${s.kneeFlexDeg} not ~${HOP_LEGS.kneeDeg}`);
  assert.ok(Math.abs(s.thighFlexDeg - 0.3 * HOP_LEGS.kneeDeg) < 4.5, `thigh flex ${s.thighFlexDeg} not ~0.3*knee`);
  // foot rides with the shin (shin swung BACK by thigh-knee) and points 15 deg further
  const shinDeg = (s.thighAngle - HOP_LEGS.kneeDeg * deg) / deg;
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
  const a = flex(0.03), b = flex(0.06), c = flex(HOP_LEGS.tuckSeamY + 0.01);
  assert.ok(a < b && b < c, `not monotonic: ${a} ${b} ${c}`);
  assert.ok(flex(0.15) - c < 1 && flex(0.5) - c < 1,
    'tuck must cap above tuckSeamY');
  // smooth in: half seam height is well under half flex (blend + smoothstep)
  assert.ok(flex(0.06) < 0.7 * HOP_LEGS.kneeDeg, `ease-in too steep: ${flex(0.06)}`);
});

test('continuity at hips_y = 0: both branches == rest (blend tolerance)', () => {
  // The planted branch now runs the FORWARD+SPLAY pole (contract: no
  // crossed shins), which places the near-straight knee a couple of mm off
  // the rest knee plane. Sub-pixel agreement is the continuity contract at
  // the crossing; exact rest at exactly-zero offset is guaranteed by the
  // player's zero-offset rest latch, not the blend.
  const s = solve(0);
  assert.equal(s.mode, 'planted');   // at/below the ground line planted owns the leg
  assert.ok(dist(s.kneePos, REST.knee) < 6e-3, `knee moved ${dist(s.kneePos, REST.knee)}`);
  assert.ok(dist(s.anklePos, REST.ankle) < 3e-3, `ankle moved ${dist(s.anklePos, REST.ankle)}`);
  assert.ok(Math.abs(s.footArc.angle) < 1e-9);
  // and NO pop across zero from either side (mm-scale agreement)
  const l = solve(-1e-4), r = solve(1e-4);
  assert.ok(dist(l.anklePos, REST.ankle) < 3e-3);
  assert.ok(dist(r.anklePos, REST.ankle) < 3e-3);
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

test('bent-FORWARD rest rig keeps its own knee direction (pole from rest)', () => {
  // knee pushed FORWARD at rest (+z, perp dot forward > poleFwdDot): the
  // solver must keep bending that way, not overwrite it with the splay
  const bent = { hip: [0, 0.9, 0], knee: [0, 0.5, 0.06], ankle: [0, 0.06, 0] };
  const s = solveHopLeg({ ...bent, forward: F, hipsY: 0 });
  assert.ok(dist(s.kneePos, bent.knee) < 1e-9, 'continuity at 0 broken for bent rest');
  const c = solveHopLeg({ ...bent, forward: F, hipsY: -0.08, side: 'left' });
  assert.ok(c.kneePos[2] > bent.knee[2], 'crouch pulled the knee back off its rest bend');
  assert.ok(c.kneeFlexDeg > s.kneeFlexDeg + 10, 'crouch did not deepen the bend');
});

test('pole: a BACK-bent rest knee no longer wins (forward+outward rule)', () => {
  // knee pushed BACK (-z): perp dot forward < 0.7 -> the crouch must knee
  // FORWARD like every other rig (the old pole kept bending backwards)
  const back = { hip: [0, 0.9, 0], knee: [0, 0.5, -0.06], ankle: [0, 0.06, 0] };
  const c = solveHopLeg({ ...back, forward: F, hipsY: -0.08 });
  assert.ok(c.thighDir[2] > 0.05, `knee went the wrong way: ${c.thighDir}`);
  assert.ok(c.kneePos[2] > back.knee[2], 'back-bent rest kept bending backwards');
});

test('knock-kneed rest rig: crouch splays knees out, never crosses the shins', () => {
  // rest knees knocked 6 cm INWARD (the sample-avatar failure mode: the
  // rest perpendicular points inward and the planted IK bent an X). L/R
  // mirrored around x=0, facing +Z.
  const knock = {
    left: { hip: [0.09, 0.90, 0], knee: [0.03, 0.48, 0.012], ankle: [0.09, 0.06, -0.005] },
    right: { hip: [-0.09, 0.90, 0], knee: [-0.03, 0.48, 0.012], ankle: [-0.09, 0.06, -0.005] },
  };
  for (const y of [-0.05, -0.10]) {
    const r = solveHopLegs({ legs: knock, forward: F, hipsY: y });
    assert.ok(r.left && r.right && r.left.mode === 'planted');
    const kneeSep = r.left.kneePos[0] - r.right.kneePos[0];
    const ankleSep = r.left.anklePos[0] - r.right.anklePos[0];
    assert.ok(kneeSep >= ankleSep - 1e-6,
      `y=${y}: knees crossed (knee sep ${kneeSep.toFixed(4)} < ankle sep ${ankleSep.toFixed(4)})`);
    for (const side of ['left', 'right']) {
      // knee in FRONT of the hip->ankle line (knees track the pole, not an X)
      const s2 = r[side];
      const line = norm(sub(s2.anklePos, [side === 'left' ? 0.09 : -0.09, 0.90 + y, 0]));
      const H = [side === 'left' ? 0.09 : -0.09, 0.90 + y, 0];
      const ko = sub(s2.kneePos, H);
      const perp = sub(ko, mul(line, dot(ko, line)));
      assert.ok(dot(perp, F) > 0.02,
        `y=${y} ${side}: knee not in front of the hip->ankle line (${perp[2].toFixed(4)})`);
    }
  }
});

// ── landing cushion (stateless doc-time curve) ──────────────────
const RECIPE_DOC = {
  name: 'cushion_recipe', duration_s: 1.5, loop: false, ease: 'smooth',
  keyframes: [
    { t: 0.0, hips_y: 0 }, { t: 0.2, hips_y: -0.08 }, { t: 0.3, hips_y: -0.08 },
    { t: 0.6, hips_y: 0.14 }, { t: 0.9, hips_y: 0 }, { t: 1.1, hips_y: -0.05 },
    { t: 1.5, hips_y: 0 },
  ],
};
const NO_RISE_DOC = {
  name: 'crouch_only', duration_s: 1.2, loop: false, ease: 'smooth',
  keyframes: [
    { t: 0.0, hips_y: 0 }, { t: 0.3, hips_y: -0.06 },
    { t: 0.6, hips_y: -0.06 }, { t: 1.2, hips_y: 0 },
  ],
};
const LEGACY_DOC = {
  name: 'legacy_leap', duration_s: 3.0, loop: false, ease: 'smooth',
  keyframes: [
    { t: 0.0, hips_y: 0 }, { t: 0.4, hips_y: -0.03 }, { t: 0.9, hips_y: 0.12 },
    { t: 1.3, hips_y: -0.02 }, { t: 1.7, hips_y: 0.02 }, { t: 2.0, hips_y: 0.06 },
    { t: 2.3, hips_y: 0.1 }, { t: 2.6, hips_y: -0.02 }, { t: 3.0, hips_y: 0 },
  ],
};

test('cushion: a doc without an airborne rise has c == 0 everywhere', () => {
  assert.deepEqual(landingTouchdowns(NO_RISE_DOC), []);
  const c = landingCushion(NO_RISE_DOC);
  for (let t = 0; t <= 1.2; t += 0.01) assert.equal(c(t), 0);
});

test('cushion: shape — 0 before t_td-0.10, C at t_td+0.06, exactly 0 at the last key', () => {
  const tds = landingTouchdowns(RECIPE_DOC);
  assert.equal(tds.length, 1, `expected 1 touchdown, got ${JSON.stringify(tds)}`);
  const td = tds[0];
  // touchdown = the sampled curve descending through ~0.01 after the 0.6 apex
  assert.ok(td > 0.8 && td < 0.91, `touchdown ${td} off the 0.9 key`);
  assert.ok(Math.abs(sampleDoc(RECIPE_DOC, td).hips_y - HOP_LEGS.crossY) < 2e-3,
    'touchdown is not the ~0.01 descent crossing');
  const c = landingCushion(RECIPE_DOC);
  assert.equal(c(0), 0);
  assert.equal(c(td - HOP_LEGS.cushionPre - 0.01), 0);
  assert.ok(Math.abs(c(td + HOP_LEGS.cushionRise) - HOP_LEGS.cushion) < 1e-12,
    `C at rise end: ${c(td + HOP_LEGS.cushionRise)}`);
  assert.equal(c(1.5), 0);                       // exactly 0 at the last keyframe
  assert.equal(c(1.6), 0);                       // past the end too
  let prev = null;
  for (let t = 0; t <= 1.5; t += 0.005) {
    const v = c(t);
    assert.ok(v >= 0 && v <= HOP_LEGS.cushion + 1e-12, `c(${t})=${v} out of range`);
    if (prev !== null && t <= td + HOP_LEGS.cushionRise) assert.ok(v >= prev - 1e-12, 'rise not monotone');
    prev = v;
  }
  // deterministic + order-independent: interleaved repeat calls are bit-equal
  const ts = [0.2, td - 0.05, td + 0.05, 1.0, 1.49];
  const a = ts.map((t) => c(t));
  ts.map((t) => landingCushion(RECIPE_DOC)(t));   // other doc interleaved
  assert.deepEqual(ts.map((t) => c(t)), a);
});

test('cushion: legacy victory_leap doc gets one cushion per airborne segment', () => {
  const tds = landingTouchdowns(LEGACY_DOC);
  assert.equal(tds.length, 2, `expected 2 touchdowns, got ${JSON.stringify(tds)}`);
  const c = landingCushion(LEGACY_DOC);
  for (const td of tds) {
    assert.ok(td > 0.9 && td < 2.9, `touchdown ${td} outside the descents`);
    assert.ok(c(td + 0.05) > 0.02, `no cushion under the ${td} landing`);
  }
  assert.equal(c(3.0), 0);
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

test('hipLive: idle-swayed hip root still plants the foot EXACTLY at rest', () => {
  // hip sways 2.4 cm sideways/forward this frame (life layer rotates hips)
  const sway = [0.018, 0.001, -0.014];
  const live = [REST.hip[0] + sway[0], REST.hip[1] + sway[1] - 0.08, REST.hip[2] + sway[2]];
  const s = solveHopLeg({ ...REST, forward: F, hipsY: -0.08, hipLive: live });
  assert.equal(s.mode, 'planted');
  // 1 cm window = the 8 mm crouch lean (heels forward); sway must NOT add drift
  assert.ok(dist(s.anklePos, REST.ankle) <= 0.01,
    `swayed hip drifted the planted foot ${dist(s.anklePos, REST.ankle).toFixed(4)} m`);
  assert.ok(Math.abs(s.anklePos[1] - REST.ankle[1]) <= 1e-6, 'planted foot left the ground plane');
  const H = [REST.hip[0], REST.hip[1] - 0.08, REST.hip[2]];
  assert.ok(Math.abs(len(sub(s.kneePos, live)) - dist(REST.hip, REST.knee)) < 1e-9);
  // near zero the target softens toward the PASSIVE foot (sway-following):
  // exact continuity with the unactuated hierarchy, no sway-scale pop
  const t = solveHopLeg({ ...REST, forward: F, hipsY: -1e-4,
    hipLive: [live[0], live[1] + 0.0799, live[2]] });
  const passive = REST.ankle.map((v, i) => v + (i === 1 ? -0.0001 : sway[i]));
  assert.ok(dist(t.anklePos, passive) < 2e-3,
    `near-zero planted snapped off the passive foot (${dist(t.anklePos, passive)})`);
  // tuck rides the live hip too (feet follow sway while airborne)
  const a = solveHopLeg({ ...REST, forward: F, hipsY: 0.14, hipLive: [live[0], live[1] + 0.22, live[2]] });
  assert.ok(Math.abs(a.anklePos[0] - REST.hip[0] - sway[0]) < 0.05);
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
