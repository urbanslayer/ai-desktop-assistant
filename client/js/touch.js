// Touch: press classification, body-part hit-testing and the
// pat/poke/tickle recogniser. Pure module (no three.js, no DOM): the page
// feeds it raw pointer facts and acts on the returned events, so every rule
// here is unit-testable under `node --test` (see touch.test.js). Contract:
// docs/TOUCH.md.
//
// Press arbitration (docs/TOUCH.md §4):
//   - press moves > MOVE_SLOP px within HOLD_MS  -> 'window-drag' (the
//     desktop drags her window; the page never grabs; a later stroke of the
//     same press can still be recognised as pat/tickle);
//   - press held still >= HOLD_MS on a grabbable part -> 'grab-start'
//     (Ctrl+press grabs immediately, no hold needed);
//   - press held still >= HOLD_MS on torso/hips -> 'hold' (nothing special;
//     a later move drags the window);
//   - quick press (< HOLD_MS, barely moved) -> 'tap' for the recogniser.
//
// Recogniser:
//   pat    = 2+ taps on the head within TAP_GAP_MS, or a back-and-forth
//            stroke whose dominant part is the head;
//   poke   = a single quick tap on the body/arm (never the head); a lone
//            head tap waits TAP_GAP_MS for a second tap, then fades away;
//   tickle = rapid (<= RAPID_MS) back-and-forth on the torso/hips.
// A stroke is a press that reverses direction >= 2 times (each leg >=
// REVERSAL_MIN_PX) while staying on her; the part visited most wins.
//
// Parts come from the same `parts` map the bbox POST carries (see index.html):
// {head, hand_l, hand_r, arm_l, arm_r, foot_l, foot_r, torso, hips} ->
// [x, y, w, h] window CSS px. Missing/absent rects are simply not hittable.

export const HOLD_MS = 250;          // still-hold that turns into a grab
export const MOVE_SLOP = 6;          // px of travel that keeps a press a window drag
export const TAP_MAX_MS = HOLD_MS;   // a press shorter than this is a tap candidate
export const TAP_GAP_MS = 700;       // max gap between chained taps (pat)
export const STROKE_MAX_MS = 1600;   // a back-and-forth slower than this is nothing
export const RAPID_MS = 850;         // at/under this a stroke reads as "rapid" (tickle on torso)
export const REVERSAL_MIN_PX = 12;   // travel a direction leg needs to count
export const HOLD_GRACE_MS = 40;     // jitter allowance when testing "still"

// Small parts first: a tap over her hand must hit 'hand_l', not the arm or
// torso rect drawn underneath it.
export const PART_PRIORITY = [
  'hand_l', 'hand_r', 'foot_l', 'foot_r',
  'arm_l', 'arm_r', 'head', 'hips', 'torso',
];

// Parts a still-hold grabs (docs/TOUCH.md: hand, forearm, foot, head).
export const GRABBABLE = new Set([
  'head', 'hand_l', 'hand_r', 'arm_l', 'arm_r', 'foot_l', 'foot_r',
]);

/** Which body-part rect contains (x, y)? Small parts win. null = empty space. */
export function hitTest(parts, x, y) {
  if (!parts) return null;
  for (const name of PART_PRIORITY) {
    const r = parts[name];
    if (!Array.isArray(r) || r.length < 4) continue;
    const [rx, ry, rw, rh] = r;
    if (!(rw > 0) || !(rh > 0)) continue;
    if (x >= rx && x <= rx + rw && y >= ry && y <= ry + rh) return name;
  }
  return null;
}

/**
 * One press, start to end. Feed it pointer facts (ms = performance.now()-ish,
 * x/y = window CSS px); it returns zero or one event per call:
 *   {type:'grab-start', part, ctrl}  the hold landed: the page starts IK
 *   {type:'window-drag', part}        early big move: the window drags, not her
 *   {type:'press-end', part, holdMs, movedPx, ctrl, kind}
 *       kind = 'tap' (quick, still: feed the recogniser),
 *              'stroke' (direction reversals: feed the recogniser, see
 *                        strokeSummary), 'grab-release' (was grabbed),
 *              'hold' (still on torso/legs: nothing)
 * Only ONE press at a time; a second down() before up() replaces it.
 */
export class PressTracker {
  constructor() {
    this.p = null;
  }

  get active() { return !!this.p; }

  /** {type:'grab-start'} when Ctrl+press lands on a grabbable part. */
  down(x, y, ms, { ctrl = false } = {}) {
    this.p = {
      x0: x, y0: y, t0: ms, part: null, movedPx: 0,
      grabbed: false, windowDrag: false, ctrl: !!ctrl,
      reversals: 0, lastDir: 0, legPx: 0, parts: [null],
    };
    // the page resolves the hit part right after down() via setPart();
    // Ctrl+press on a grabbable part returns grab-start from there
    return null;
  }

  /** Page resolves the hit part (hitTest on the live `parts` map) and
   *  re-drives the press start. Returns grab-start for an instant Ctrl grab. */
  setPart(part) {
    if (!this.p || this.p.part != null) return null;
    this.p.part = part ?? null;
    this.p.parts[0] = this.p.part;
    if (this.p.ctrl && GRABBABLE.has(this.p.part)) {
      this.p.grabbed = true;
      return { type: 'grab-start', part: this.p.part, ctrl: true };
    }
    return null;
  }

  move(x, y, ms) {
    const p = this.p;
    if (!p) return null;
    const dx = x - (p.x1 ?? p.x0), dy = y - (p.y1 ?? p.y0);
    p.x1 = x; p.y1 = y;
    const step = Math.hypot(dx, dy);
    p.movedPx = Math.max(p.movedPx, Math.hypot(x - p.x0, y - p.y0));
    // direction-reversal bookkeeping for stroke recognition (relative to the
    // last reversal point, horizontal axis — strokes read as back-and-forth)
    this._trackStroke(p, dx);
    if (!p.windowDrag && !p.grabbed) {
      if (p.movedPx > MOVE_SLOP && ms - p.t0 <= HOLD_MS + HOLD_GRACE_MS) {
        p.windowDrag = true;              // the window drags; never a grab
        return { type: 'window-drag', part: p.part };
      } else if (ms - p.t0 >= HOLD_MS && GRABBABLE.has(p.part)) {
        p.grabbed = true;                 // still-hold on a limb: grab it
        return { type: 'grab-start', part: p.part, ctrl: p.ctrl };
      }
    }
    return null;
  }

  up(ms) {
    const p = this.p;
    if (!p) return null;
    this.p = null;
    const holdMs = ms - p.t0;
    const movedWhileStill = p.movedPx <= MOVE_SLOP + HOLD_GRACE_MS;
    let kind;
    if (p.grabbed) kind = 'grab-release';
    else if (holdMs < TAP_MAX_MS && movedWhileStill) kind = 'tap';
    else if (p.reversals >= 2 && holdMs <= STROKE_MAX_MS) kind = 'stroke';
    else if (p.windowDrag) kind = 'drag';
    else kind = 'hold';
    return {
      type: 'press-end', kind, part: p.part,
      holdMs, movedPx: p.movedPx, ctrl: p.ctrl,
      reversals: p.reversals,
    };
  }

  /** Current grab state (mid-hold), for the page's pointerup fallback. */
  get grabbed() { return this.p?.grabbed ? this.p.part : null; }

  _trackStroke(p, dx) {
    const dir = dx > 0.5 ? 1 : dx < -0.5 ? -1 : 0;
    if (dir === 0) return;
    if (p.lastDir === 0) { p.lastDir = dir; p.legPx = Math.abs(dx); return; }
    if (dir === p.lastDir) { p.legPx += Math.abs(dx); return; }
    if (p.legPx >= REVERSAL_MIN_PX) {
      p.reversals++;
      p.lastDir = dir;
      p.legPx = Math.abs(dx);
    } else {
      // noise wobble inside the current leg; keep accumulating
      p.legPx += Math.abs(dx);
    }
  }
}

/**
 * Pat / poke / tickle recogniser. Pure state over completed presses:
 *   tap(part, ms)     -> event | null
 *   stroke(part, ms, reversals) -> event | null
 * Events: {kind:'pat'|'poke'|'tickle', part}. A lone head tap is remembered
 * for TAP_GAP_MS (a second one makes it a pat); after the gap it expires
 * silently via expire(ms) (which never emits).
 */
export class TouchRecogniser {
  constructor() {
    this._lastTapMs = -1e9;
    this._lastTapPart = null;
  }

  tap(part, ms) {
    if (part === 'head') {
      if (ms - this._lastTapMs <= TAP_GAP_MS && this._lastTapPart === 'head') {
        this._reset();
        return { kind: 'pat', part: 'head' };
      }
      this._lastTapMs = ms;
      this._lastTapPart = 'head';
      return null;                       // waiting for a second tap
    }
    this._reset();
    if (part == null) return null;
    // single quick click on the body/arm (not the head) -> poke
    return { kind: 'poke', part };
  }

  stroke(part, ms, reversals = 0) {
    this._reset();                       // a stroke interrupts a tap chain
    if (!part || reversals < 2) return null;
    if (part === 'head') return { kind: 'pat', part };        // stroking her head
    if (part === 'torso' || part === 'hips') {
      // rapid back-and-forth on the torso is tickling; a slow one is a pat-ish
      // stroke the spec only defines for the head — say nothing
      return ms <= RAPID_MS ? { kind: 'tickle', part } : null;
    }
    return null;
  }

  expire(ms) {
    if (this._lastTapPart && ms - this._lastTapMs > TAP_GAP_MS) this._reset();
  }

  _reset() {
    this._lastTapMs = -1e9;
    this._lastTapPart = null;
  }
}

/**
 * /control/touch debouncer (docs/TOUCH.md): same part+kind at most once per
 * DEBOUNCE_MS; grab/release always pass. touch-mode 'off' mutes everything.
 */
export const TOUCH_DEBOUNCE_MS = 1500;

export class TouchPostGate {
  constructor({ debounceMs = TOUCH_DEBOUNCE_MS, now = () => Date.now() } = {}) {
    this.debounceMs = debounceMs;
    this.now = now;
    this.last = new Map();   // "part:kind" -> ms
  }

  allow(part, kind) {
    if (kind === 'grab' || kind === 'release') return true;
    const key = `${part}:${kind}`;
    const ms = this.now();
    const at = this.last.get(key);
    if (at != null && ms - at < this.debounceMs) return false;
    this.last.set(key, ms);
    return true;
  }
}
