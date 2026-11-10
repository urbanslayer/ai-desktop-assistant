// node --test client/js/touch.js — press arbitration, hit-testing and the
// pat/poke/tickle recogniser (docs/TOUCH.md). Pure module, no three needed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  hitTest, PressTracker, TouchRecogniser, TouchPostGate, PART_PRIORITY,
  HOLD_MS, MOVE_SLOP, TAP_GAP_MS, RAPID_MS,
} from './touch.js';

// a plausible parts map (window CSS px), non-overlapping except hand inside arm
const PARTS = {
  head:  [180, 40, 60, 70],
  hand_l: [236, 300, 28, 28],
  hand_r: [136, 300, 28, 28],
  arm_l: [224, 180, 40, 150],
  arm_r: [136, 180, 40, 150],
  foot_l: [216, 600, 34, 30],
  foot_r: [150, 600, 34, 30],
  torso: [140, 120, 120, 200],
  hips:  [150, 320, 100, 60],
};

test('hitTest: point inside a rect hits it; small parts win over big ones', () => {
  assert.equal(hitTest(PARTS, 210, 70), 'head');
  assert.equal(hitTest(PARTS, 150, 250), 'arm_r');      // arm rect beats the torso rect under it
  assert.equal(hitTest(PARTS, 150, 310), 'hand_r');      // hand rect overlapping the torso band wins
  assert.equal(hitTest(PARTS, 200, 450), null);          // between hips and feet = empty
  assert.equal(hitTest(PARTS, 200, 350), 'hips');
  assert.equal(hitTest(null, 1, 1), null);
  assert.equal(hitTest({ head: [0, 0, 0, 0] }, 0, 0), null);  // zero-size not hittable
  for (const p of PART_PRIORITY) assert.ok(hitTest(PARTS, ...cornerOf(p)), `must hit ${p}`);
});
function cornerOf(name) {
  const [x, y, w, h] = PARTS[name];
  return [x + w / 2, y + h / 2];
}

test('press held still >= 250 ms on a limb grabs it (move -> window drag before)', () => {
  const t = new PressTracker();
  t.down(150, 310, 0);
  assert.equal(t.setPart('hand_r'), null);
  assert.equal(t.move(151, 310, 120), null);          // tiny jitter: still pending
  assert.equal(t.move(150, 311, 240), null);          // not yet 250 ms
  assert.deepEqual(t.move(150, 311, 260),
    { type: 'grab-start', part: 'hand_r', ctrl: false });
  assert.equal(t.grabbed, 'hand_r');
  const e = t.up(900);
  assert.equal(e.kind, 'grab-release');
  assert.equal(e.holdMs, 900);
});

test('press moving > 6 px within 250 ms stays a window drag forever', () => {
  const t = new PressTracker();
  t.down(150, 310, 0);
  t.setPart('hand_r');
  assert.deepEqual(t.move(170, 310, 100), { type: 'window-drag', part: 'hand_r' });
  // hmm: window-drag marker must not be emitted as an event twice; check idempotence
  assert.equal(t.move(180, 310, 150), null);
  assert.equal(t.grabbed, null);
  const e = t.up(600);
  assert.equal(e.kind, 'drag');
});

test('Ctrl+press on a limb grabs immediately, no hold needed', () => {
  const t = new PressTracker();
  t.down(210, 70, 0, { ctrl: true });
  assert.deepEqual(t.setPart('head'), { type: 'grab-start', part: 'head', ctrl: true });
  const e = t.up(40);
  assert.equal(e.kind, 'grab-release');
  assert.equal(e.holdMs, 40);
});

test('Ctrl+press on torso is nothing special', () => {
  const t = new PressTracker();
  t.down(200, 200, 0, { ctrl: true });
  assert.equal(t.setPart('torso'), null);
  assert.equal(t.up(50).kind, 'tap');       // quick ctrl tap on torso: a poke candidate
});

test('press held still on torso/hips -> hold (no grab, no reaction)', () => {
  const t = new PressTracker();
  t.down(200, 200, 0);
  t.setPart('torso');
  assert.equal(t.move(200, 201, 300), null);   // still, but torso is not grabbable
  assert.equal(t.up(700).kind, 'hold');
});

test('still press released just under 250 ms reads as a tap', () => {
  const t = new PressTracker();
  t.down(150, 310, 0);
  t.setPart('hand_r');
  const e = t.up(110);
  assert.equal(e.kind, 'tap');
  assert.equal(e.holdMs, 110);
});

test('back-and-forth press counts direction reversals', () => {
  const t = new PressTracker();
  t.down(160, 200, 0);
  t.setPart('torso');
  // go right 60, left 60, right 60, left 60 in 10 px steps
  let x = 160, ms = 16;
  for (const seq of [1, -1, 1, -1]) {
    for (let i = 0; i < 6; i++) { x += seq * 10; t.move(x, 200, ms); ms += 16; }
  }
  const e = t.up(ms);
  assert.equal(e.kind, 'stroke');
  assert.ok(e.reversals >= 3, `expected reversals, got ${e.reversals}`);
});

test('recogniser: two quick head taps = pat, spaced too far apart = nothing', () => {
  const r = new TouchRecogniser();
  assert.equal(r.tap('head', 100), null);            // first tap waits
  assert.deepEqual(r.tap('head', 100 + TAP_GAP_MS - 10), { kind: 'pat', part: 'head' });
  assert.equal(r.tap('head', 5000), null);
  r.expire(5000 + TAP_GAP_MS + 10);                  // lone tap expires silently
  assert.equal(r.tap('head', 20000), null);
  assert.equal(r.tap('head', 20000 + TAP_GAP_MS + 5), null);  // expired: still alone
  r.expire(99999);
});

test('recogniser: single click on body/arm = poke, never for the head', () => {
  const r = new TouchRecogniser();
  assert.deepEqual(r.tap('arm_r', 100), { kind: 'poke', part: 'arm_r' });
  assert.deepEqual(r.tap('torso', 300), { kind: 'poke', part: 'torso' });
  assert.deepEqual(r.tap('hips', 400), { kind: 'poke', part: 'hips' });
  assert.equal(r.tap(null, 500), null);
  assert.equal(r.tap('head', 600), null);            // head alone is not a poke
});

test('recogniser: strokes — head = pat, rapid torso = tickle, slow torso = nothing', () => {
  const r = new TouchRecogniser();
  assert.deepEqual(r.stroke('head', 900, 2), { kind: 'pat', part: 'head' });
  assert.deepEqual(r.stroke('head', 400, 3), { kind: 'pat', part: 'head' });
  assert.deepEqual(r.stroke('torso', RAPID_MS - 50, 3), { kind: 'tickle', part: 'torso' });
  assert.equal(r.stroke('torso', 1200, 3), null);    // slow torso stroke: undefined gesture
  assert.equal(r.stroke('arm_l', 300, 2), null);     // arm strokes: nothing defined
  assert.equal(r.stroke('torso', 300, 1), null);     // too few reversals
});

test('post gate: same part+kind 1.5 s; grab/release always; off mutes at the page', () => {
  let nowMs = 0;
  const g = new TouchPostGate({ now: () => nowMs });
  assert.equal(g.allow('head', 'pat'), true);
  assert.equal(g.allow('head', 'pat'), false);
  nowMs = 1400;
  assert.equal(g.allow('head', 'pat'), false);
  nowMs = 1600;
  assert.equal(g.allow('head', 'pat'), true);
  assert.equal(g.allow('head', 'poke'), true);       // different kind passes
  for (let i = 0; i < 5; i++) assert.equal(g.allow('hand_r', 'grab'), true);
  for (let i = 0; i < 5; i++) assert.equal(g.allow('hand_r', 'release'), true);
});
