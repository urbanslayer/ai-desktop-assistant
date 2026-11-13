import { test } from 'node:test';
import assert from 'node:assert/strict';
import { placeBubble, overlapArea, BubbleTracker } from './bubble.js';

// A typical desktop window (character_config.yaml desktop.width/height).
const VIEW = { w: 420, h: 680 };
// Sample rig at scale 1.0 in that window (measured, see the bubble QA work):
// head band ~ y 150..215, face (eyes..chin) ~ 165..205, torso band ~ 215..330.
const HEAD = { x: 160, y: 150, w: 100, h: 65 };
const FACE = { x: 172, y: 165, w: 76, h: 40 };
const BODY = { x: 140, y: 215, w: 140, h: 115 };
const SHORT = { w: 340, h: 55 };      // one line
const MED = { w: 340, h: 110 };       // 3-4 lines
const LONG = { w: 340, h: 400 };      // overflowing text

const base = (over = {}) => ({
  headRect: HEAD, faceRect: FACE, bodyRect: BODY,
  view: VIEW, bubble: SHORT, ...over,
});

test('above fits: centred on the head, bottom >= gap above head top', () => {
  const p = placeBubble(base({ bubble: SHORT }));
  assert.equal(p.mode, 'above');
  assert.ok(p.y + p.h <= HEAD.y - 8 + 0.001, 'bottom clears the head top');
  assert.ok(Math.abs((p.x + p.w / 2) - (HEAD.x + HEAD.w / 2)) < 0.001, 'centred on head');
  assert.equal(p.tail.side, 'down');
  assert.equal(overlapArea({ x: p.x, y: p.y, w: p.w, h: p.h }, FACE), 0);
});

test('above does not fit -> body below the chin, never over the face', () => {
  // head pushed to the top of the window: no room above it
  const p = placeBubble(base({ headRect: { x: 160, y: 30, w: 100, h: 65 },
                               faceRect: { x: 172, y: 45, w: 76, h: 40 },
                               bubble: MED }));
  assert.equal(p.mode, 'body');
  assert.ok(p.y >= 45 + 40, 'top edge is below the chin');
  assert.equal(overlapArea({ x: p.x, y: p.y, w: p.w, h: p.h },
                            { x: 172, y: 45, w: 76, h: 40 }), 0);
  assert.equal(p.tail.side, 'up');
  assert.ok(p.y + p.h <= BODY.y + BODY.h, 'stays above the hips');
});

test('tiny window: neither above nor body fits -> top (old behaviour)', () => {
  // head at the very top (no room above), torso band cut off by the short
  // window (no room below the chin either)
  const p = placeBubble(base({
    view: { w: 200, h: 120 }, bubble: MED,
    headRect: { x: 60, y: 20, w: 80, h: 55 },
    faceRect: { x: 70, y: 35, w: 60, h: 35 },
    bodyRect: { x: 50, y: 75, w: 100, h: 255 },
  }));
  assert.equal(p.mode, 'top');
  assert.equal(p.y, 8);
  assert.ok(p.h <= 120 * 0.32 + 0.001);
});

test('clamping: head at the window edge keeps the bubble inside with margins', () => {
  for (const headX of [-60, 400]) {
    const p = placeBubble(base({ headRect: { x: headX, y: 150, w: 100, h: 65 } }));
    assert.equal(p.mode, 'above');
    assert.ok(p.x >= 8 - 0.001 && p.x + p.w <= VIEW.w - 8 + 0.001,
      `inside window with margins (headX=${headX}): ${p.x}+${p.w}`);
  }
  // narrow window forces the bubble width down, still inside
  const p = placeBubble(base({ view: { w: 150, h: 680 }, bubble: { w: 340, h: 55 } }));
  assert.equal(p.mode, 'above');
  assert.ok(p.x >= 8 - 0.001 && p.x + p.w <= 150 - 8 + 0.001);
});

test('face never overlapped across a sweep of head positions (above/body)', () => {
  for (let headY = 0; headY < 600; headY += 10) {
    const headRect = { x: 160, y: headY, w: 100, h: 65 };
    const faceRect = { x: 172, y: headY + 15, w: 76, h: 40 };
    const bodyRect = { x: 140, y: headY + 65, w: 140, h: 115 };
    const p = placeBubble(base({ headRect, faceRect, bodyRect, bubble: MED }));
    assert.ok(['above', 'body', 'top'].includes(p.mode));
    if (p.mode === 'above' || p.mode === 'body') {
      assert.equal(overlapArea({ x: p.x, y: p.y, w: p.w, h: p.h }, faceRect), 0,
        `mode=${p.mode} headY=${headY}`);
    }
    // always fully inside the canvas
    assert.ok(p.x >= 0 && p.y >= 0 && p.x + p.w <= VIEW.w && p.y + p.h <= VIEW.h,
      `inside canvas mode=${p.mode} headY=${headY}`);
  }
});

test('long text in above mode: capped at the available space (caller scrolls)', () => {
  const p = placeBubble(base({ bubble: LONG }));
  assert.equal(p.mode, 'above');
  assert.ok(p.h <= p.maxH + 0.001);
  assert.ok(p.maxH <= HEAD.y - 8 - 8 + 0.001);
});

test('force: infeasible mode reports none (feeds the page fallback chain)', () => {
  const p = placeBubble(base({ headRect: { x: 160, y: 20, w: 100, h: 65 }, force: 'above' }));
  assert.equal(p.mode, 'none');
});

test('top-left/bottom-right rect shapes both accepted', () => {
  const a = placeBubble(base({ headRect: { top: 150, bottom: 215, left: 160, right: 260 },
                               faceRect: { top: 165, bottom: 205, left: 172, right: 248 },
                               bodyRect: { top: 215, bottom: 330, left: 140, right: 280 } }));
  const b = placeBubble(base());
  assert.deepEqual(a, b);
});

// ── BubbleTracker: hysteresis + low-pass ──────────────────────────────
const modes = (above, body) => ({ modes: { ...(above ? { above } : {}), ...(body ? { body } : {}) } });
const A = { mode: 'above', x: 40, y: 60, w: 340, h: 55 };
const B = { mode: 'body', x: 40, y: 230, w: 340, h: 55 };

test('hysteresis: an infeasible mode is abandoned at once, but the re-appearance of a better mode waits out the hold (no flip-flop)', () => {
  const t = new BubbleTracker({ holdS: 0.5 });
  assert.equal(t.decide(modes(A, B), 0).mode, 'above');
  assert.equal(t.decide(modes(null, B), 0.2).mode, 'body', 'above gone -> body immediately');
  assert.equal(t.decide(modes(A, B), 0.3).mode, 'body', 'above back too soon: hold body');
  assert.equal(t.decide(modes(A, B), 0.8).mode, 'above', 'switch once the hold elapsed');
});

test('low-pass: position eases toward the target and snaps within 0.5 px', () => {
  const t = new BubbleTracker({ alpha: 0.3 });
  assert.equal(t.decide(modes(A), 0).y, 60);
  const p1 = t.decide(modes({ ...A, y: 160 }), 0.1);
  assert.ok(p1.y > 60 && p1.y < 160, `eased, not jumped: ${p1.y}`);
  let y = p1.y;
  for (let i = 0; i < 60; i++) y = t.decide(modes({ ...A, y: 160 }), 1 + i * 0.1).y;
  assert.equal(y, 160, 'snapped to the target');
});
