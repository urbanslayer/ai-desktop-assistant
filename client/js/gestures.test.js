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
  for (let i = 0; i < 3; i++) assert.ok(Math.abs(c.leftLowerArm[i] - d.leftLowerArm[i]) < 0.05);
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

test('wave wrist + arm oscillators swing and ease in/out', () => {
  const s0 = sampleGesture('wave', 0.4);
  const hand = [], arm = [];
  for (let t = 0.55; t <= 2.05; t += 0.05) {
    const s = sampleGesture('wave', t);
    hand.push(s.rightHand[2]);
    arm.push(s.rightUpperArm[2]);
  }
  const handSpread = Math.max(...hand) - Math.min(...hand);
  const armSpread = Math.max(...arm) - Math.min(...arm);
  assert.ok(handSpread > 0.5, `wrist should flap, got ${handSpread}`);
  assert.ok(armSpread > 0.3, `arm should rock, got ${armSpread}`);
  // 2-3 cycles over the ~1.55 s window (1.5 Hz): count wrist reversals
  let reversals = 0;
  for (let i = 1; i < hand.length - 1; i++) {
    if ((hand[i] - hand[i - 1]) * (hand[i + 1] - hand[i]) < 0) reversals++;
  }
  assert.ok(reversals >= 3, `expected several wrist reversals, got ${reversals}`);
  // near the window edges the wobble is eased out
  assert.ok(Math.abs(sampleGesture('wave', 0.56).rightHand[2] - s0.rightHand[2]) < 0.05);
  assert.ok(Math.abs(sampleGesture('wave', 0.56).rightUpperArm[2] - s0.rightUpperArm[2]) < 0.05);
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
