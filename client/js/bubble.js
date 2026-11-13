// Speech-bubble placement (docs/TOUCH.md "Speech bubble placement").
//
// The old bubble was pinned to the top of the window — which, because the
// desktop window is framed tightly around her body, meant it sat ON her
// face. This module decides where the bubble goes each frame from three
// projected boxes (CSS px):
//
//   headRect  her head (skull top incl. hair/hat pad .. chin)
//   faceRect  the eyes..chin band the bubble must never cover
//   bodyRect  chest..hips torso band (the fallback resting place)
//
// Preference order:
//   1. 'above' — over her head, bottom edge >= gap px above the head top,
//      horizontally centred on the head, clamped inside the window with
//      `margin` px margins. maxH = the space available; the CALLER may
//      shrink the font one step and retry to make it fit (the page does
//      that, then re-calls with the measured size).
//   2. 'body'  — over the chest/torso, top edge below the chin + gap, so it
//      never covers eyes/mouth; maxH = space down to the hips.
//   3. 'top'   — last resort (today's old behaviour: pinned to the top of
//      the window) only when the window is too small for both.
//
// A tail/pointer x-offset toward her head is returned (side: 'down' when
// above, 'up' when on the body) so the CSS triangle can point at her.
//
// Pure module (like scale.js): no DOM, no three.js — `node --test` covers
// the geometry, and BubbleTracker below adds the temporal half (mode
// hysteresis + position low-pass so hops/sways/zoom don't make it flip or
// jump) which is equally DOM-free and testable.

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Accept {top,bottom,left,right} or {x,y,w,h}; normalise to {x,y,w,h}. */
function normRect(r) {
  if (!r) return null;
  if (r.w != null && r.h != null) {
    return { x: r.x, y: r.y, w: r.w, h: r.h };
  }
  return { x: r.left, y: r.top, w: r.right - r.left, h: r.bottom - r.top };
}

/** Axis-aligned overlap area of two normalised rects (0 when disjoint). */
export function overlapArea(a, b) {
  if (!a || !b) return 0;
  const A = normRect(a), B = normRect(b);
  const ox = Math.min(A.x + A.w, B.x + B.w) - Math.max(A.x, B.x);
  const oy = Math.min(A.y + A.h, B.y + B.h) - Math.max(A.y, B.y);
  return ox > 0 && oy > 0 ? ox * oy : 0;
}

/**
 * placeBubble({ headRect, faceRect, bodyRect, view:{w,h},
 *               bubble:{w,h},            // measured at the current width
 *               margin = 8, gap = 8,     // window margin / clearance above head or chin
 *               lineH = 20.3, padV = 20, // one text line / vertical padding (small step: lineH 17.6)
 *               minLines = 2, force })   // force: 'above'|'body'|'top' to test one mode
 *
 * → { mode:'above'|'body'|'top'|'none', x, y, w, h, maxH, tail:{x,side}|null }
 * (x,y = top-left of the bubble; h = min(bubble.h, maxH); tail.x is px from
 * the bubble's LEFT edge. 'none' only when `force`d to an infeasible mode;
 * without force, 'top' is always returned as the last resort.)
 */
export function placeBubble(opts) {
  const { view, bubble } = opts;
  const margin = opts.margin ?? 8;
  const gap = opts.gap ?? 8;
  const lineH = opts.lineH ?? 20.3;
  const padV = opts.padV ?? 20;
  const minLines = opts.minLines ?? 2;
  const minNeeded = padV + minLines * lineH;   // ~2 readable lines
  const head = normRect(opts.headRect) || {};
  const face = normRect(opts.faceRect) || {};
  const body = normRect(opts.bodyRect) || {};
  const W = Math.min(bubble.w, view.w - 2 * margin);
  const headCx = head.x != null ? head.x + head.w / 2 : view.w / 2;
  const fitX = () => clamp(headCx - W / 2, margin, Math.max(margin, view.w - margin - W));

  function fit(mode) {
    if (mode === 'above') {
      const bottom = (head.y ?? 0) - gap;          // bubble bottom clears head top
      const space = bottom - margin;
      if (head.y == null || space < minNeeded) return null;
      const h = Math.min(bubble.h, space);
      const x = fitX();
      return { mode, x, y: bottom - h, w: W, h, maxH: space,
               tail: { x: clamp(headCx - x, 16, Math.max(16, W - 16)), side: 'down' } };
    }
    if (mode === 'body') {
      const top = (face.y ?? 0) + (face.h ?? 0) + gap;   // strictly below the chin
      const bottomLimit = Math.min(body.y != null ? body.y + body.h : view.h, view.h - margin);
      const space = bottomLimit - top;
      if (face.y == null || space < minNeeded) return null;
      const h = Math.min(bubble.h, space);
      const x = fitX();
      return { mode, x, y: top, w: W, h, maxH: space,
               tail: { x: clamp(headCx - x, 16, Math.max(16, W - 16)), side: 'up' } };
    }
    // 'top': the old pinned-to-top behaviour.
    const h = Math.min(bubble.h, view.h * 0.32);
    return { mode: 'top', x: fitX(), y: margin, w: W, h,
             maxH: Math.floor(view.h * 0.32), tail: null };
  }

  if (opts.force) return fit(opts.force) || { mode: 'none', maxH: 0 };
  return fit('above') || fit('body') || fit('top');
}

/**
 * Temporal half (the page calls place()/reset() once per frame while the
 * bubble is shown, with the candidate map it computed for THIS frame):
 *
 *   decide({ modes: {above?, body?, top?} }, nowS)
 *     - preference: above > body > top (only feasible modes are in the map);
 *     - an INFEASIBLE current mode is abandoned at once (the bubble must not
 *       sit on her head); a better mode that REAPPEARS must wait out
 *       `holdS` (0.5 s) before taking over — so a hop that briefly eats the
 *       headroom does not flip above/body/above per frame;
 *     - low-pass: the returned x/y ease toward the target (alpha per call)
 *       and snap within 0.5 px, so sway/hop motion never jumps.
 */
export class BubbleTracker {
  constructor({ holdS = 0.5, alpha = 0.3 } = {}) {
    this.holdS = holdS;
    this.alpha = alpha;
    this.mode = null;
    this.switchAt = -Infinity;
    this.px = null;
  }
  reset() { this.mode = null; this.px = null; this.switchAt = -Infinity; }
  decide({ modes }, nowS = 0) {
    const prefer = modes.above || modes.body || modes.top;
    if (!prefer) return null;
    let chosen = prefer;
    if (this.mode && this.mode !== prefer.mode
        && nowS - this.switchAt < this.holdS && modes[this.mode]) {
      chosen = modes[this.mode];   // hold the current mode a little longer
    }
    if (chosen.mode !== this.mode) {
      this.mode = chosen.mode;
      this.switchAt = nowS;
      this.px = null;              // a real switch teleports (it reads as a decision)
    }
    if (!this.px) this.px = { x: chosen.x, y: chosen.y };
    else {
      for (const k of ['x', 'y']) {
        const d = chosen[k] - this.px[k];
        this.px[k] = Math.abs(d) <= 0.5 ? chosen[k] : this.px[k] + d * this.alpha;
      }
    }
    return { ...chosen, x: this.px.x, y: this.px.y };
  }
}
