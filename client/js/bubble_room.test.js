import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  roomNeeded, BubbleRoomTracker,
  ROOM_STEP, ROOM_MAX, ROOM_GAP, ROOM_MARGIN, ROOM_RELEASE_MS, ROOM_ASK_MS,
} from './bubble_room.js';

// The SAME table tests/test_bubble_room.py asserts on the Python side
// (riko/bubble_room.py): the page and the desktop must never disagree about
// how much room a bubble needs. Head-top y values are the measured ones:
// 44 px on the sample rig, 24 px on the owner rig at the default 420x680.
const SAMPLE = { headTopY: 44 };
const OWNER = { headTopY: 24 };

test('constants match the desktop module', () => {
  assert.equal(ROOM_STEP, 16);
  assert.equal(ROOM_MAX, 220);
  assert.equal(ROOM_GAP, 8);
  assert.equal(ROOM_MARGIN, 8);
  assert.equal(ROOM_RELEASE_MS, 1500);
});

test('roomNeeded: exactly-fitting text asks for nothing', () => {
  // the above slot is headTopY - gap - margin = 104 - 16 = 88 px
  assert.equal(roomNeeded({ headTopY: 104, bubbleH: 88 }), 0);
  assert.equal(roomNeeded({ headTopY: 104, bubbleH: 89 }), 16);
});

test('roomNeeded: measured rigs, step rounding, ceiling, junk', () => {
  assert.equal(roomNeeded({ ...SAMPLE, bubbleH: 61 }), 48);   // 61+16-44 = 33 -> 48
  assert.equal(roomNeeded({ ...OWNER, bubbleH: 61 }), 64);    // 53 -> 64
  assert.equal(roomNeeded({ ...OWNER, bubbleH: 89 }), 96);    // 81 -> 96
  assert.equal(roomNeeded({ ...SAMPLE, bubbleH: 400 }), ROOM_MAX);
  assert.equal(roomNeeded({ ...SAMPLE, bubbleH: 400, max: 96 }), 96);
  for (const junk of [null, undefined, NaN, '', 'abc', Infinity]) {
    assert.equal(roomNeeded({ headTopY: junk, bubbleH: 100 }), 0);
    assert.equal(roomNeeded({ headTopY: 44, bubbleH: junk }), 0);
  }
  assert.equal(roomNeeded({}), 0);
  assert.equal(roomNeeded(), 0);
});

test('tracker: first unfeasible frame asks once, repeats are silent', () => {
  const t = new BubbleRoomTracker();
  assert.equal(t.frame({ nowMs: 1000, shown: true, ...SAMPLE, bubbleH: 61,
                         aboveFits: false }), 48);
  // same need, no re-POST inside the ask window (no compositor churn)
  assert.equal(t.frame({ nowMs: 1030, shown: true, ...SAMPLE, bubbleH: 61,
                         aboveFits: false }), null);
  assert.equal(t.frame({ nowMs: 1100, shown: true, ...SAMPLE, bubbleH: 61,
                         aboveFits: false }), null);
  assert.equal(t.target, 48);
});

test('tracker: grows further for a longer line, never shrinks mid-bubble', () => {
  const t = new BubbleRoomTracker({ askMs: 1e9 });   // keep-alive off: pure grow logic
  assert.equal(t.frame({ nowMs: 0, shown: true, ...SAMPLE, bubbleH: 61 }), 48);
  assert.equal(t.frame({ nowMs: 10, shown: true, ...SAMPLE, bubbleH: 200 }), 176);
  // a short follow-up line while she is still talking: no shrink, no POST
  assert.equal(t.frame({ nowMs: 20, shown: true, ...SAMPLE, bubbleH: 61 }), null);
  assert.equal(t.target, 176);
  assert.equal(t.frame({ nowMs: 30, shown: true, ...SAMPLE, bubbleH: 61,
                         aboveFits: true }), null);
  assert.equal(t.target, 176);
});

test('tracker: ceiling caps the ask for a 6-sentence reply', () => {
  const t = new BubbleRoomTracker();
  assert.equal(t.frame({ nowMs: 0, shown: true, ...OWNER, bubbleH: 600 }), ROOM_MAX);
});

test('tracker: keep-alive re-asks slowly while shown (self-heals a FIT/drag)', () => {
  const t = new BubbleRoomTracker();
  assert.equal(t.frame({ nowMs: 0, shown: true, ...SAMPLE, bubbleH: 61 }), 48);
  assert.equal(t.frame({ nowMs: ROOM_ASK_MS - 1, shown: true, ...SAMPLE,
                         bubbleH: 61, aboveFits: true }), null);
  assert.equal(t.frame({ nowMs: ROOM_ASK_MS, shown: true, ...SAMPLE,
                         bubbleH: 61, aboveFits: true }), 48);
  assert.equal(t.frame({ nowMs: ROOM_ASK_MS * 2, shown: true, ...SAMPLE,
                         bubbleH: 61, aboveFits: true }), 48);
});

test('tracker: release only after the quiet delay, exactly once', () => {
  const t = new BubbleRoomTracker();
  t.frame({ nowMs: 0, shown: true, ...SAMPLE, bubbleH: 61 });
  assert.equal(t.frame({ nowMs: 1000, shown: false }), null);        // waiting
  assert.equal(t.frame({ nowMs: 1000 + ROOM_RELEASE_MS - 1, shown: false }), null);
  assert.equal(t.frame({ nowMs: 1000 + ROOM_RELEASE_MS, shown: false }), 0);
  // one release per bubble: later idle frames say nothing
  assert.equal(t.frame({ nowMs: 1000 + ROOM_RELEASE_MS + 500, shown: false }), null);
  assert.equal(t.target, 0);
});

test('tracker: consecutive lines do not pump the window', () => {
  const t = new BubbleRoomTracker();
  assert.equal(t.frame({ nowMs: 0, shown: true, ...SAMPLE, bubbleH: 61 }), 48);
  // her reply ends for one frame, the next line arrives 600 ms later
  assert.equal(t.frame({ nowMs: 1000, shown: false }), null);
  const again = t.frame({ nowMs: 1600, shown: true, ...SAMPLE, bubbleH: 61 });
  // nothing to gain: at most a keep-alive with the SAME number (a no-op for
  // the desktop) — never a new grow, never a shrink
  assert.ok(again === null || again === 48, `reused, not re-grown (got ${again})`);
  assert.equal(t.target, 48);
  assert.equal(t.frame({ nowMs: 2000, shown: false }), null);
  assert.equal(t.frame({ nowMs: 2000 + ROOM_RELEASE_MS, shown: false }), 0);  // once
  assert.equal(t.frame({ nowMs: 2000 + ROOM_RELEASE_MS + 10, shown: false }), null);
});

test('tracker: a bubble that fits above never touches the window', () => {
  const t = new BubbleRoomTracker();
  assert.equal(t.frame({ nowMs: 0, shown: true, headTopY: 300, bubbleH: 61,
                         aboveFits: true }), null);
  assert.equal(t.target, 0);
  assert.equal(t.frame({ nowMs: 99999, shown: false }), null);   // nothing to release
});

test('tracker: reset (persona swap / harness) forgets the room', () => {
  const t = new BubbleRoomTracker();
  t.frame({ nowMs: 0, shown: true, ...SAMPLE, bubbleH: 61 });
  t.reset();
  assert.equal(t.target, 0);
  assert.equal(t.frame({ nowMs: 10, shown: false }), null);
  assert.equal(t.frame({ nowMs: 20, shown: true, ...SAMPLE, bubbleH: 61 }), 48);
});

test('tracker: junk frames are ignored, not acted on', () => {
  const t = new BubbleRoomTracker();
  assert.equal(t.frame({}), null);
  assert.equal(t.frame({ nowMs: NaN, shown: true, bubbleH: 61 }), null);
  assert.equal(t.frame({ nowMs: 5, shown: true, headTopY: 44, bubbleH: 61 }), 48);
  // headTopY unknown -> roomNeeded is 0 -> no grow, no shrink
  assert.equal(t.frame({ nowMs: 6, shown: true, headTopY: null, bubbleH: 61 }), null);
});
