// Wheel -> avatar scale steps, the 0.4–2.5 clamp, the Ctrl+scroll camera
// dolly (unchanged from the old wheel-zoom), and the smoothing glide.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  SCALE_MIN, SCALE_MAX, SCALE_STEP, clampScale, stepScale,
  dollyDistance, approachScale,
} from './scale.js';

test('wheel steps multiply by ~8% per notch', () => {
  let s = 1.0;
  for (let i = 0; i < 10; i++) s = stepScale(s, -100);   // 10 notches up
  assert.ok(Math.abs(s - Math.pow(SCALE_STEP, 10)) < 1e-9, s);
  let d = 1.0;
  for (let i = 0; i < 10; i++) d = stepScale(d, 100);    // 10 notches down
  assert.ok(Math.abs(d - Math.pow(1 / SCALE_STEP, 10)) < 1e-9, d);
});

test('scale clamps at the band edges; extra scroll is a no-op', () => {
  let s = 1.0;
  for (let i = 0; i < 60; i++) s = stepScale(s, -120);   // way past 2.5
  assert.equal(s, SCALE_MAX);
  s = stepScale(s, -100);                                // stops growing
  assert.equal(s, SCALE_MAX);
  for (let i = 0; i < 60; i++) s = stepScale(s, 120);    // way past 0.4
  assert.equal(s, SCALE_MIN);
  s = stepScale(s, 100);
  assert.equal(s, SCALE_MIN);
});

test('junk deltas and junk scales never poison the number', () => {
  assert.equal(stepScale(1.0, 0), 1.0);
  assert.equal(stepScale(1.0, NaN), 1.0);
  assert.equal(stepScale(undefined, -100), SCALE_STEP);  // junk current -> 1.0
  assert.equal(clampScale('2.2'), 2.2);
  assert.equal(clampScale(99), SCALE_MAX);
  assert.equal(clampScale('nonsense'), 1.0);
});

test('ctrl+scroll is the old camera dolly and never moves the scale', () => {
  // wheel down moves the camera away (zoom out), wheel up closes in —
  // clamped to OrbitControls' minDistance/maxDistance band
  assert.ok(dollyDistance(2.0, 100, 0.5, 6) > 2.0);
  assert.ok(dollyDistance(2.0, -100, 0.5, 6) < 2.0);
  assert.equal(dollyDistance(0.5, -100, 0.5, 6), 0.5);   // at minDistance: stuck
  assert.equal(dollyDistance(6.0, 100, 0.5, 6), 6.0);    // at maxDistance: stuck
  assert.equal(dollyDistance(2.0, 0, 0.5, 6), 2.0);      // no notch, no move
  // the two channels are independent: dollying never steps the scale and
  // stepping the scale never moves the camera
  assert.equal(stepScale(1.0, -100), SCALE_STEP);        // scale moved...
  assert.equal(dollyDistance(2.0, 100, 0.5, 6) * (1 / 1.12), 2.0);  // ...camera distance not
});

test('reset returns exactly 1.0 (the page assigns it, not a step)', () => {
  assert.equal(clampScale(1.0), 1.0);
  assert.equal(approachScale(2.5, 1.0, 5.0), 1.0);       // glide lands exactly
});

test('the glide moves toward the target, is frame-rate independent, snaps', () => {
  const up = approachScale(1.0, 2.0, 1 / 60);
  assert.ok(up > 1.0 && up < 2.0, up);
  // two small steps ≈ one big step (exp smoothing), loose tolerance
  let a = 1.0, b = 1.0;
  for (let i = 0; i < 2; i++) a = approachScale(a, 2.0, 1 / 30);
  b = approachScale(b, 2.0, 2 / 30);
  assert.ok(Math.abs(a - b) < 0.02, `${a} vs ${b}`);
  assert.equal(approachScale(1.999, 2.0, 1 / 60), 2.0);  // snap inside eps
  assert.equal(approachScale(1.5, 1.5, 1 / 60), 1.5);    // converged: no churn
  assert.equal(approachScale(1.0, 'junk', 1 / 60), 1.0); // junk target -> 1.0
});
