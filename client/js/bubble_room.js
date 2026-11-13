// Bubble headroom (docs/TOUCH.md "Speech bubble headroom") — the request
// logic for making room ABOVE her head while she talks.
//
// client/js/bubble.js decides WHERE the bubble goes; at the default
// 420x680 desktop window there are only 8-28 px above her head, so `above`
// never fits and the bubble always lands on her chest. The owner's fix:
// while the bubble is shown and `above` does not fit, ask the desktop for
// the missing px of window ABOVE her head (POST /control/bubble-room
// {extra}); the desktop grows the window upward (y -= extra, h += extra) and
// the page re-frames with pixels-per-metre and the feet pixel position
// preserved, so the new space appears exactly where it is needed and she
// never changes size. When the bubble has been gone for releaseMs, ask for
// 0 and the window goes back to its normal rect.
//
// Rules encoded here (all of them node-tested, DOM-free):
//   * needs are rounded UP to ROOM_STEP px and capped at ROOM_MAX — a few
//     grows per bubble at most, never one per frame of sway;
//   * while shown, the target NEVER shrinks (a shorter line does not take
//     the room back mid-speech);
//   * exactly ONE release per bubble, and only after releaseMs of quiet:
//     consecutive lines/replies arriving inside that window reuse the room
//     instead of pumping the window up and down;
//   * a slow keep-alive re-ask (askMs) while shown: the desktop answers
//     'already' without touching the compositor, and an external resize
//     (a zoom FIT, a drag) is repaired on the next ask; a grant that landed
//     a step short (her head moved between ask and grant) is topped up by
//     adding the room already granted to the freshly measured shortfall.
//
// The numbers mirror riko/bubble_room.py 1:1 (room_needed / STEP / MAX /
// GAP / MARGIN) — both sides are unit-tested against the same table.

/** Request granularity / ceiling (px). */
export const ROOM_STEP = 16;
export const ROOM_MAX = 220;
/** Must match bubblePlacement's clearance numbers in client/index.html. */
export const ROOM_GAP = 8;
export const ROOM_MARGIN = 8;
/**
 * placeBubble only takes the above-head slot when it can show at least
 * `minLines` readable lines (padV + 2 * lineH at the readable font:
 * 20 + 2 * 20.3 = 60.6 => 61). Asking for less than that buys a window that
 * STILL puts the bubble on her chest, so the request floor is this tall even
 * for a one-line bubble.
 */
export const ROOM_MIN_H = 61;
/** Wait this long after the bubble hides before giving the room back. */
export const ROOM_RELEASE_MS = 1500;
/** Re-ask at most this often while the bubble is shown (keep-alive). */
export const ROOM_ASK_MS = 500;

// Strict like the Python side: null/undefined/'', booleans, NaN and Infinity
// are "no number" (Number(null) === 0 is the trap this avoids).
function num(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/**
 * Extra px of window above her head so a bubble of `bubbleH` really lands in
 * the `above` slot. Two floors decide it: the bubble itself, and
 * placeBubble's "at least 2 readable lines over the head" rule (`minH`) —
 * the slot must be `max(bubbleH, minH) + gap` above the head top, inside
 * `margin` px of window margin. So the shortfall is
 * `max(bubbleH, minH) + gap + margin - headTopY`, rounded up to `step` and
 * capped at `max`. 0 when it already fits (or the inputs are junk).
 */
export function roomNeeded({ headTopY, bubbleH, minH = ROOM_MIN_H,
                             gap = ROOM_GAP, margin = ROOM_MARGIN,
                             step = ROOM_STEP, max = ROOM_MAX } = {}) {
  const head = num(headTopY), hRaw = num(bubbleH), floor = num(minH) ?? ROOM_MIN_H;
  if (head === null || hRaw === null) return 0;
  const h = Math.max(hRaw, floor);
  const need = h + (num(gap) ?? ROOM_GAP) + (num(margin) ?? ROOM_MARGIN) - head;
  if (need <= 0) return 0;
  const s = Math.max(1, num(step) ?? ROOM_STEP);
  return Math.min(Math.max(0, num(max) ?? ROOM_MAX), Math.ceil(need / s) * s);
}

/**
 * One tracker per page (the page has one bubble). The render loop calls
 * frame() every frame with what THIS frame's placement decided:
 *
 *   frame({ nowMs, shown, headTopY, bubbleH, aboveFits, granted }) → null | px
 *
 * A number is what to POST to /control/bubble-room right now (0 = give the
 * room back); null means say nothing. It never decides anything about her
 * size or position — that is the desktop's and the camera's job.
 */
export class BubbleRoomTracker {
  constructor({ step = ROOM_STEP, max = ROOM_MAX, releaseMs = ROOM_RELEASE_MS,
                askMs = ROOM_ASK_MS } = {}) {
    this.step = step;
    this.max = max;
    this.releaseMs = releaseMs;
    this.askMs = askMs;
    this.target = 0;         // px we are holding (what we last asked for)
    this.shown = false;
    this.grantedSeen = -1;   // last grant the desktop reported (change detector)
    this.releaseAt = null;   // wall ms at which an idle bubble releases
    this.askAt = -Infinity;  // last POST time (debounce + keep-alive clock)
  }

  reset() {
    this.target = 0;
    this.shown = false;
    this.grantedSeen = -1;
    this.releaseAt = null;
    this.askAt = -Infinity;
  }

  frame({ nowMs, shown, headTopY = null, bubbleH = 0, minH = ROOM_MIN_H,
          aboveFits = false, granted = 0 } = {}) {
    const now = num(nowMs);
    if (now === null) return null;
    if (!shown) {
      this.shown = false;
      if (this.target <= 0) return null;
      if (this.releaseAt === null) this.releaseAt = now + this.releaseMs;
      if (now < this.releaseAt) return null;
      this.target = 0;                 // exactly one release per bubble
      this.releaseAt = null;
      this.askAt = now;
      return 0;
    }
    this.shown = true;
    this.releaseAt = null;             // back within the window: no release, no pump
    if (!aboveFits) {
      // `granted` is what the window ALREADY grew by: headTopY is measured in
      // the grown canvas, so roomNeeded() now returns the ADDITIONAL px the
      // slot still lacks. Her head bobs a few px between the ask and the
      // grant, so the first grant can land a step short; without adding the
      // room we already hold, the recomputed need would look smaller than the
      // target and the bubble would stay on her chest forever.
      const g = Math.max(0, Math.round(num(granted) ?? 0));
      const add = roomNeeded({ headTopY, bubbleH, minH, step: this.step,
                               max: this.max - g });
      const want = Math.min(this.max, g + add);
      if (want > this.target) {        // grow further (never shrink while shown)
        this.target = want;
        this.askAt = now;
        this.grantedSeen = g;
        return this.target;
      }
      // The grant UNDER-delivered (monitor clamp, or her head bobbed between
      // ask and grow): re-assert the target ONCE per changed answer, so a
      // desktop that cannot give more is not spammed every frame — the slow
      // keep-alive below carries any repair of an external resize.
      if (g !== this.grantedSeen) {
        this.grantedSeen = g;
        if (this.target > g) {
          this.askAt = now;
          return this.target;
        }
      }
    }
    if (this.target > 0 && now - this.askAt >= this.askMs) {
      this.askAt = now;
      return this.target;              // keep-alive (idempotent desktop-side)
    }
    return null;
  }
}
