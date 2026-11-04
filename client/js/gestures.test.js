// node --test client/js/gestures.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GESTURES, POSE_BONES, samplePose, sampleGesture, mirrorPose, vowelWeights } from './gestures.js';

const ALL = Object.keys(GESTURES);

test('one-shot gestures start and end at the rest pose (no snap)', () => {
  for (const name of ALL) {
    const g = GESTURES[name];
    const a = g.keys[0].bones, b = g.keys[g.keys.length - 1].bones;
    if (!g.loop) {  // the player cross-fades loops, one-shots fade by themselves
      assert.deepEqual(a, {}, `${name} start not rest`);
      assert.deepEqual(b, {}, `${name} end not rest`);
    } else {  // loops must return to their opening pose
      for (const bone of Object.keys(b)) {
        assert.deepEqual(b[bone], a[bone] || [0, 0, 0], `${name} loop mismatch on ${bone}`);
      }
    }
  }
});

test('keyframes are sorted, inside the duration, and cover the whole span', () => {
  for (const name of ALL) {
    const g = GESTURES[name];
    assert.equal(g.keys[0].t, 0, name);
    assert.equal(g.keys[g.keys.length - 1].t, g.duration, name);
    for (let i = 1; i < g.keys.length; i++) assert.ok(g.keys[i].t > g.keys[i - 1].t, `${name} t=${i}`);
    for (const k of g.keys) {
      for (const [bone, rot] of Object.entries(k.bones)) {
        assert.ok(POSE_BONES.includes(bone), `${name}: ${bone}`);
        assert.equal(rot.length, 3, name);
        for (const v of rot) assert.ok(Number.isFinite(v) && Math.abs(v) < 3.2, `${name} ${bone} ${v}`);
      }
    }
  }
});

test('samplePose interpolates smoothly and eases', () => {
  const mid = samplePose('nod', 0.125);  // between {} and neck x 0.22
  assert.ok(mid.neck[0] > 0 && mid.neck[0] < 0.22);
  assert.equal(samplePose('nod', 0.25).neck[0].toFixed(6), '0.220000');
  assert.equal(samplePose('nod', -1).neck ? samplePose('nod', -1).neck[0] : 0, 0);  // clamped
  assert.ok(samplePose('nod', 999));  // clamped at end = rest
  // smoothstep: quarter point is below linear
  const q = samplePose('nod', 0.0625).neck[0];
  assert.ok(q < 0.11, `easing should lag linear at quarter point (${q})`);
});

test('samplePose loops wrap continuously', () => {
  const a = samplePose('think', 3.99), b = samplePose('think', 4.01);
  for (const bone of Object.keys(a)) {
    for (let i = 0; i < 3; i++) assert.ok(Math.abs(a[bone][i] - b[bone][i]) < 0.05);
  }
  const c = samplePose('think', -0.01), d = samplePose('think', 3.99);
  // (the arms are IK-driven now — ik.test.js checks the arm loop wrap on the
  // wrist targets; euler-wise the shared head/neck keys must match)
  for (let i = 0; i < 3; i++) assert.ok(Math.abs(c.neck[i] - d.neck[i]) < 0.05);
});

test('gesture table has the set ROUNDS.md asks for', () => {
  for (const n of ['wave', 'think', 'listen', 'stretch', 'look_around', 'fidget', 'hair_touch',
                   'talk_1', 'talk_2', 'talk_3', 'nod', 'shake_head', 'laugh', 'pout']) {
    assert.ok(GESTURES[n], n);
  }
  assert.equal(GESTURES.think.loop, true);
  assert.equal(GESTURES.listen.loop, true);
  assert.equal(GESTURES.wave.loop, false);
});

test('wave wrist flap + IK arm rock ease in/out', () => {
  const s0 = sampleGesture('wave', 0.4);
  const hand = [];
  for (let t = 0.55; t <= 2.05; t += 0.05) {
    const s = sampleGesture('wave', t);
    hand.push(s.rightHand[2]);
  }
  const handSpread = Math.max(...hand) - Math.min(...hand);
  assert.ok(handSpread > 0.5, `wrist should flap, got ${handSpread}`);
  // the arm rock lives on the IK target track now (see ik.test.js for the
  // swing measured against synthetic rigs); it must exist and be generous
  const armOsc = GESTURES.wave.arms.right.osc;
  assert.ok(Array.isArray(armOsc) && armOsc.length, 'wave must rock the arm via the IK target');
  assert.ok(armOsc[0].amp > 0.1 && armOsc[0].axis === 0, `arm rock spec: ${JSON.stringify(armOsc)}`);
  // 2-3 cycles over the ~1.55 s window (1.5 Hz): count wrist reversals
  let reversals = 0;
  for (let i = 1; i < hand.length - 1; i++) {
    if ((hand[i] - hand[i - 1]) * (hand[i + 1] - hand[i]) < 0) reversals++;
  }
  assert.ok(reversals >= 3, `expected several wrist reversals, got ${reversals}`);
  // near the window edges the wobble is eased in: at t=0.56 (0.06 s into the
  // 0.25 s edge ramp) the flap+base must still be near the rest angle
  assert.ok(Math.abs(sampleGesture('wave', 0.56).rightHand[2]) < 0.12);
  // and the IK arm track is at rest at t=0 / back at rest at the end
  assert.ok(!GESTURES.wave.arms.right.keys[0].target, 'wave must start from the rest arm pose');
  assert.ok(!GESTURES.wave.arms.right.keys.at(-1).target, 'wave must end at the rest arm pose');
});

test('IK-driven gestures carry arm tracks that loop-match', () => {
  for (const name of ['wave', 'think']) {
    const g = GESTURES[name];
    assert.ok(g.arms?.right, `${name} needs a right arm track`);
    assert.equal(g.arms.right.keys[0].t, 0, name);
    assert.equal(g.arms.right.keys.at(-1).t, g.duration, name);
    if (g.loop) {  // loop poses must hold across the wrap
      for (const side of Object.keys(g.arms)) {
        const k = g.arms[side].keys;
        assert.deepEqual(k[0].target, k.at(-1).target, `${name}.${side} loop mismatch`);
        assert.deepEqual(k[0].pole, k.at(-1).pole, `${name}.${side} pole loop mismatch`);
      }
    }
  }
});

test('mirrorPose flips upperArm z for VRM 0.x only', () => {
  const p = { rightUpperArm: [-0.15, 0, -0.45], neck: [0.1, 0, 0] };
  assert.deepEqual(mirrorPose(p, true), p);
  const m = mirrorPose(p, false);
  assert.deepEqual(m.rightUpperArm, [-0.15, 0, 0.45]);
  assert.deepEqual(m.neck, [0.1, 0, 0]);
  assert.notEqual(m.rightUpperArm, p.rightUpperArm);  // a copy, not the same array
});

// ── vowel lip sync math ──────────────────────────────────────────
const SR = 48000, N = 1024;
const spectrum = (peakHz, db = -20) => {
  const f = new Float32Array(N).fill(-100);
  const bin = Math.round((peakHz / (SR / 2)) * N);
  for (let i = Math.max(1, bin - 3); i <= Math.min(N - 1, bin + 3); i++) f[i] = db;
  return f;
};

test('vowelWeights: silence is a closed mouth', () => {
  const w = vowelWeights(new Float32Array(N).fill(-100), SR);
  assert.equal(w.open, 0);
  for (const v of ['aa', 'ih', 'ou', 'ee', 'oh']) assert.equal(w[v], 0);
});

test('vowelWeights: picks the vowel matching the spectral centroid', () => {
  for (const [v, hz] of [['ou', 300], ['oh', 550], ['aa', 850], ['ih', 1500], ['ee', 2500]]) {
    const w = vowelWeights(spectrum(hz), SR);
    assert.equal(w[v], w.open, `${v} should win at ${hz} Hz (open=${w.open})`);
    for (const other of ['aa', 'ih', 'ou', 'ee', 'oh']) {
      if (other !== v) assert.equal(w[other], 0, `${other} must be 0 when ${v} wins`);
    }
    assert.ok(w.open > 0.5, `${v} openness`);
  }
});

test('vowelWeights: loudness drives openness', () => {
  const quiet = vowelWeights(spectrum(850, -55), SR);
  const loud = vowelWeights(spectrum(850, -12), SR);
  assert.ok(quiet.open < 0.5 && loud.open > 0.9, `${quiet.open} ${loud.open}`);
});
