// node --test client/js/touch.js — press arbitration, hit-testing and the
// pat/poke/tickle recogniser (docs/TOUCH.md). Pure module, no three needed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  hitTest, PressTracker, TouchRecogniser, TouchPostGate, PART_PRIORITY,
  HOLD_MS, MOVE_SLOP, TAP_GAP_MS, RAPID_MS, bboxSeamChanged, SeamPostPolicy,
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

// ── non-roam desktop bbox seam (docs/TOUCH.md) ───────────────────────────
// The seam payload the page posts in NON-roam desktop mode must NOT need
// director.walk (bboxBody(cx) is walk-free by construction); these tests
// cover the pure posting policy the page drives it through.

const seamPayload = (over = {}) => ({
  x: 169, y: 0, w: 162, h: 537, hips_y: 311, feet_y: 511,
  parts: {
    head: [225, 104, 50, 54], hand_l: [286, 254, 45, 44], hand_r: [169, 254, 45, 44],
    arm_l: [274, 206, 55, 90], arm_r: [171, 206, 55, 90],
    foot_l: [239, 463, 52, 49], foot_r: [209, 463, 52, 49],
    torso: [215, 150, 70, 140], hips: [221, 279, 58, 65],
  },
  ...over,
});

test('bboxSeamChanged: <2 px jitter is not a change; >=2 px anywhere is', () => {
  assert.equal(bboxSeamChanged(seamPayload(), seamPayload()), false);
  assert.equal(bboxSeamChanged(seamPayload(), seamPayload({ hips_y: 312 })), false);
  assert.equal(bboxSeamChanged(seamPayload(), seamPayload({ hips_y: 313 })), true);
  assert.equal(bboxSeamChanged(seamPayload(), seamPayload({ w: 164 })), true);
  const moved = seamPayload();
  moved.parts.hand_l = [286, 256, 45, 44];              // +2 px in a part rect
  assert.equal(bboxSeamChanged(seamPayload(), moved), true);
  const grew = seamPayload();
  grew.parts.head = [225, 104, 51, 54];                 // +1 px: still quiet
  assert.equal(bboxSeamChanged(seamPayload(), grew), false);
});

test('bboxSeamChanged: unknown <-> known and added/removed parts are changes', () => {
  assert.equal(bboxSeamChanged(seamPayload(), seamPayload({ hips_y: null })), true);
  assert.equal(bboxSeamChanged(seamPayload({ hips_y: null }), seamPayload()), true);
  const noHead = seamPayload();
  delete noHead.parts.head;
  assert.equal(bboxSeamChanged(seamPayload(), noHead), true);   // part vanished
  assert.equal(bboxSeamChanged(noHead, seamPayload()), true);   // part appeared
  assert.equal(bboxSeamChanged(null, seamPayload()), true);
});

test('SeamPostPolicy: first post always; then only >=2 px moves or 1 s beats', () => {
  const p = new SeamPostPolicy({ minPx: 2, repostS: 1.0 });
  assert.equal(p.shouldPost(seamPayload(), 0.0), true);        // first sample
  assert.equal(p.shouldPost(seamPayload(), 0.1), false);       // idle: quiet
  assert.equal(p.shouldPost(seamPayload(), 0.9), false);
  assert.equal(p.shouldPost(seamPayload({ hips_y: 300 }), 0.95), true);  // drag: 11 px
  assert.equal(p.shouldPost(seamPayload({ hips_y: 300 }), 1.04), false); // settled again
  assert.equal(p.shouldPost(seamPayload({ hips_y: 300 }), 1.90), false); // 0.95 s: no
  assert.equal(p.shouldPost(seamPayload({ hips_y: 300 }), 2.01), true);  // 1 s heartbeat
  p.reset();
  assert.equal(p.shouldPost(seamPayload(), 5.0), true);        // persona swap re-posts
});
