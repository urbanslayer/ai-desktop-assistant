// Wheel -> avatar scale steps (delta-continuous), the 0.4–2.5 clamp, the
// Ctrl+scroll camera dolly (unchanged), the smoothing glide, the gesture
// machine (headroom grow / one fit), and the framing invariants that make
// the window's grow/fit invisible: pixels-per-metre depends only on the
// visual scale, and the feet pixel offset only on FOOT_MARGIN_PX.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  SCALE_MIN, SCALE_MAX, SCALE_STEP, WHEEL_NOTCH, GESTURE_IDLE_MS,
  GESTURE_HEADROOM, FOOT_MARGIN_PX, clampScale, stepScale,
  dollyDistance, approachScale, headroomWindow, GestureTracker,
  worldHalfHeight, pixelsPerMetre, framingDistance, feetAnchorTarget,
} from './scale.js';

test('clampScale: band, junk -> 1.0', () => {
  assert.equal(clampScale(0.1), SCALE_MIN);
  assert.equal(clampScale(99), SCALE_MAX);
  assert.equal(clampScale(1.7), 1.7);
  assert.equal(clampScale('2.2'), 2.2);
  for (const junk of [undefined, null, NaN, 'x', {}]) assert.equal(clampScale(junk), 1.0);
});

test('stepScale: one notch ~ SCALE_STEP, both directions', () => {
  let s = 1.0;
  for (let i = 0; i < 10; i++) s = stepScale(s, -WHEEL_NOTCH);   // 10 notches up
  assert.ok(Math.abs(s - Math.pow(SCALE_STEP, 10)) < 1e-9, s);
  let d = 1.0;
  for (let i = 0; i < 10; i++) d = stepScale(d, WHEEL_NOTCH);    // 10 notches down
  assert.ok(Math.abs(d - Math.pow(1 / SCALE_STEP, 10)) < 1e-9, d);
  // a 120-px mouse notch is a bit more than one step — smooth, not chunky
  assert.ok(stepScale(1.0, -120) > stepScale(1.0, -WHEEL_NOTCH));
});

test('stepScale: trackpad deltas are proportional (continuous, not per-event)', () => {
  const small = 5;                       // typical trackpad pixel delta
  const many = 20;                       // one trackpad flick = 20 events
  let s = 1.0;
  for (let i = 0; i < many; i++) s = stepScale(s, -small);
  assert.ok(Math.abs(s - Math.pow(SCALE_STEP, (small * many) / WHEEL_NOTCH)) < 1e-9);
  // two 5-px events === one 10-px event (composable deltas)
  const two = stepScale(stepScale(1.0, -small), -small);
  assert.ok(Math.abs(two - stepScale(1.0, -2 * small)) < 1e-12);
});

test('stepScale: deltaMode line/page scaled, clamped at the band, junk safe', () => {
  assert.ok(Math.abs(stepScale(1.0, -1, 1) - stepScale(1.0, -33)) < 1e-12);
  assert.ok(Math.abs(stepScale(1.0, -1, 2) - stepScale(1.0, -400)) < 1e-12);
  let s = 1.0;
  for (let i = 0; i < 60; i++) s = stepScale(s, -120);   // way past 2.5
  assert.equal(s, SCALE_MAX);
  assert.equal(stepScale(s, -WHEEL_NOTCH), SCALE_MAX);   // stops growing
  for (let i = 0; i < 60; i++) s = stepScale(s, 120);    // way past 0.4
  assert.equal(s, SCALE_MIN);
  assert.equal(stepScale(s, WHEEL_NOTCH), SCALE_MIN);
  assert.equal(stepScale(1.5, 0), 1.5);
  assert.equal(stepScale(1.5, NaN), 1.5);
  assert.equal(stepScale(undefined, -WHEEL_NOTCH), SCALE_STEP);  // junk current -> 1.0
});

test('ctrl+scroll is the old camera dolly and never moves the scale', () => {
  assert.ok(dollyDistance(2.0, 100, 0.5, 6) > 2.0);        // wheel down: away
  assert.ok(dollyDistance(2.0, -100, 0.5, 6) < 2.0);       // wheel up: closer
  assert.equal(dollyDistance(0.5, -100, 0.5, 6), 0.5);     // at minDistance: stuck
  assert.equal(dollyDistance(6.0, 100, 0.5, 6), 6.0);      // at maxDistance: stuck
  assert.equal(dollyDistance(2.0, 0, 0.5, 6), 2.0);        // no notch, no move
  // the two channels stay independent
  assert.equal(stepScale(1.0, -100), SCALE_STEP);
  assert.equal(dollyDistance(2.0, 100, 0.5, 6) * (1 / 1.12), 2.0);
});

test('reset returns exactly 1.0 (the page assigns it, not a step)', () => {
  assert.equal(clampScale(1.0), 1.0);
  assert.equal(approachScale(2.5, 1.0, 5.0), 1.0);       // glide lands exactly
});

test('approachScale: glides continuously and lands exactly (~100 ms feel)', () => {
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
  // a 1.0 -> 1.4 glide is continuous, monotone and lands in ~6 frames-worth of tau
  let s = 1.0, prev = 1.0, frames = 0;
  while (s !== 1.4 && frames < 300) {
    s = approachScale(s, 1.4, 1 / 60);
    assert.ok(s >= prev - 1e-12 && s <= 1.4 + 1e-12);
    prev = s;
    frames++;
  }
  assert.equal(s, 1.4);
  assert.ok(frames >= 10 && frames <= 40, `glide took ${frames} frames`);
  let d = 1.4;
  for (let i = 0; i < 300 && d !== 1.0; i++) d = approachScale(d, 1.0, 1 / 60);
  assert.equal(d, 1.0);
  assert.equal(approachScale(NaN, 1.2, 0.016), 1.2);
});

test('headroomWindow: target x GESTURE_HEADROOM', () => {
  assert.ok(Math.abs(headroomWindow(1.25) - 1.25 * GESTURE_HEADROOM) < 1e-12);
});

test('GestureTracker: first wheel grows once, mid-gesture wheels do not move the window', () => {
  const g = new GestureTracker();
  // fresh gesture from window 1.0 toward 1.4: one grow to 1.4*1.6 = 2.24
  const grow = g.wheel(1000, 1.4, 1.0);
  assert.ok(Math.abs(grow - 1.4 * GESTURE_HEADROOM) < 1e-12);
  // the wheel keeps scrolling toward the same window: no further moves
  for (let t = 1033; t <= 1300; t += 33) {
    assert.equal(g.wheel(t, 1.2 + (t - 1000) / 2000, grow), null, `t=${t}`);
  }
  // ...until the target outgrows the headroom: then ONE more step up
  const grow2 = g.wheel(1350, 2.0, grow);
  assert.ok(grow2 > grow);
  assert.equal(g.wheel(1380, 2.0, grow2), null);
});

test('GestureTracker: a 4-notch burst grows ONCE, then once with slack — not per notch', () => {
  const g = new GestureTracker();
  const grows = [];
  let win = 1.0, s = 1.0;
  for (let i = 0; i < 4; i++) {                 // 4 mouse notches 60 ms apart
    s = stepScale(s, -120);
    const gr = g.wheel(1000 + i * 60, s, win);
    if (gr !== null) { grows.push(gr); win = gr; }
  }
  // notch 1: headroom 1.0967 x 1.6 = 1.7548; notch 2 edges past the
  // deadband and pads to 1.7548 x GROW_SLACK = 2.4567, which covers the
  // whole rest of the burst (final headroom is 2.315).
  assert.deepEqual(grows.map((v) => Math.round(v * 1000)), [1755, 2457]);
  assert.ok(win >= s * GESTURE_HEADROOM * 0.99);  // final target still fits
});

test('GestureTracker: a fine-grained trackpad swipe grows ~twice, not 20 times', () => {
  const g = new GestureTracker();
  let win = 1.0, s = 1.0, grows = 0;
  for (let i = 0; i < 20; i++) {                // 20 trackpad deltas = 2.4 notches
    s = stepScale(s, -12);
    const gr = g.wheel(i * 40, s, win);
    if (gr !== null) { win = gr; grows += 1; }
  }
  assert.ok(grows <= 3, `expected <= 3 grows, got ${grows}`);
  assert.ok(win >= s * GESTURE_HEADROOM * 0.99);
});

test('GestureTracker: fit fires exactly once per gesture, idleMs after the last wheel', () => {
  const g = new GestureTracker();
  assert.equal(g.tick(500, 1.0), null);            // never scrolled: idle
  g.wheel(1000, 1.4, 1.0);
  assert.equal(g.tick(1000 + GESTURE_IDLE_MS - 1, 1.4), null);
  assert.equal(g.tick(1000 + GESTURE_IDLE_MS, 1.4), 1.4);   // the fit, once
  assert.equal(g.tick(1000 + GESTURE_IDLE_MS + 100, 1.4), null);
  // a wheel before the idle line postpones the fit
  const g2 = new GestureTracker();
  g2.wheel(0, 1.2, 1.0);
  assert.equal(g2.tick(GESTURE_IDLE_MS - 50, 1.2), null);
  g2.wheel(GESTURE_IDLE_MS - 50, 1.25, 1.92);
  assert.equal(g2.tick(GESTURE_IDLE_MS + 100, 1.25), null);
  assert.equal(g2.tick(2 * GESTURE_IDLE_MS, 1.25), 1.25);
});

test('GestureTracker: shrink gestures skip the grow, reset() arms a fresh gesture', () => {
  const g = new GestureTracker();
  assert.equal(g.wheel(0, 0.6, 1.0), null);        // smaller: window already has room
  assert.equal(g.tick(GESTURE_IDLE_MS, 0.6), 0.6); // still fits (shrinks the window)
  g.reset();
  assert.equal(g.tick(10 * GESTURE_IDLE_MS, 0.6), null);
  assert.ok(g.wheel(10 * GESTURE_IDLE_MS, 1.5, 0.6) > 0.6);
});

test('framing invariant: pixels-per-metre depends only on visual scale, never on the canvas size', () => {
  const fitDist = 5.0, fov = 30;               // fit-to-body distance in the base window
  const baseH = 680;
  for (const v of [0.4, 0.85, 1.0, 1.4, 2.5]) {
    const ppmBase = pixelsPerMetre(baseH, fitDist, fov) * v;
    for (const H of [300, 680, 1088, 1600, 2720]) {   // fit and headroom canvases
      const d = framingDistance(fitDist, H, baseH, v);
      const ppm = pixelsPerMetre(H, d, fov);
      assert.ok(Math.abs(ppm / ppmBase - 1) < 1e-9, `v=${v} H=${H}`);
    }
  }
});

test('framing invariant: feet sit FOOT_MARGIN_PX above the canvas bottom at any scale and canvas size', () => {
  const fitDist = 5.0, fov = 30, baseH = 680, yFeet = 0.02;
  for (const v of [0.4, 1.0, 1.7, 2.5]) {
    for (const H of [340, 680, 1360]) {
      const d = framingDistance(fitDist, H, baseH, v);
      const yt = feetAnchorTarget(yFeet, d, fov, H);
      const half = worldHalfHeight(d, fov);
      const ndcFeet = (yFeet - yt) / half;
      const screenY = ((1 - ndcFeet) / 2) * H;
      assert.ok(Math.abs((H - screenY) - FOOT_MARGIN_PX) < 1e-6, `v=${v} H=${H}`);
    }
  }
});
