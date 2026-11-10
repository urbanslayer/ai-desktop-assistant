// Plain scroll over her = her SIZE, not the camera (desktop mode).
//
// Smoothness contract (owner feedback: compositor-resize-driven zoom feels
// like delayed jumps): the PAGE renders the zoom itself, every frame — the
// camera re-frames to a VISUAL scale that glides in ~100 ms, so she grows
// and shrinks continuously regardless of the window. The real window moves
// ONCE per gesture in each direction: the first wheel event asks the desktop
// for a HEADROOM window (target x GESTURE_HEADROOM, feet anchored, monitor-
// clamped desktop-side; riko/avatar_scale.py does the geometry), a mid-
// gesture target that outgrows the pad re-grows in ONE step of at least
// GROW_SLACK, and GESTURE_IDLE_MS after the wheel stops the window FITS to
// the final scale.
// Because pixels-per-metre depends only on the visual scale
// (framingDistance/pixelsPerMetre below), re-framing on the canvas resize
// keeps her on-screen size AND her feet pixel position — the fit is
// invisible. Pure module like walk.js — no three.js, no DOM — so
// `node --test` covers the wheel math, the gesture machine and the framing.
//
// Ctrl+scroll keeps the OLD behaviour: a camera dolly (close-up crop) for
// those who want it; that math lives here too so both paths are tested.
// Double-click resets the scale to 1.0 (and the page re-fits the camera).

/** The allowed band and the per-notch step (~8%). */
export const SCALE_MIN = 0.4;
export const SCALE_MAX = 2.5;
export const SCALE_STEP = 1.08;
export const DOLLY_STEP = 1.12;   // camera dolly factor per notch (old OrbitControls feel)

/** Wheel pixels that make one full notch (a mouse click is 100..120). */
export const WHEEL_NOTCH = 100;
/** No wheel event for this long = the gesture ended; fit the window now. */
export const GESTURE_IDLE_MS = 400;
/** Window headroom requested at gesture start: target x this factor. */
export const GESTURE_HEADROOM = 1.6;
/** A mid-gesture re-grow requests at least window x this — one big pad
 *  instead of one resize per notch (a 120 px mouse notch steps ~9.7% and a
 *  trackpad fires dozens of small deltas; without slack every one of them
 *  edges past the deadband and churns the compositor). 1.4 covers ~4
 *  notches per grow. */
export const GROW_SLACK = 1.4;
/** Feet sit this many px above the window's bottom edge (constant across
 *  resizes: the window bottom is feet-anchored, so she never moves). */
export const FOOT_MARGIN_PX = 14;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Sanitise a persisted/seeded scale: finite, inside the band, else 1.0. */
export function clampScale(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') {
    return 1.0;   // Number(null) is 0 — missing must be 1.0, not the band floor
  }
  const n = Number(value);
  if (!Number.isFinite(n)) return 1.0;
  return clamp(n, SCALE_MIN, SCALE_MAX);
}

/**
 * One wheel event on the scale: deltaY < 0 (wheel up / two fingers up)
 * makes her bigger. The response is CONTINUOUS in the delta — one notch
 * (~100 px, one mouse click) is SCALE_STEP (~8%), a trackpad's many small
 * deltas move her proportionally instead of a full step per event. Line-
 * based deltas (Firefox) are scaled to pixel equivalents first. Clamped to
 * the band; scrolling more when clamped stops growing, nothing breaks.
 */
export function stepScale(current, deltaY, deltaMode = 0) {
  const s = clampScale(current);
  let dy = Number(deltaY);
  if (!Number.isFinite(dy) || dy === 0) return s;
  if (Number(deltaMode) === 1) dy *= 33;        // DOM_DELTA_LINE
  else if (Number(deltaMode) === 2) dy *= 400;  // DOM_DELTA_PAGE
  return clamp(s * Math.pow(SCALE_STEP, -dy / WHEEL_NOTCH), SCALE_MIN, SCALE_MAX);
}

/**
 * Ctrl+scroll: the camera dolly distance for OrbitControls' min/max band.
 * deltaY > 0 (wheel down) moves the camera away (wider shot); it never
 * touches the avatar scale, and the scale never feeds back into it.
 */
export function dollyDistance(distance, deltaY, minDistance, maxDistance) {
  const d = Number(distance);
  if (!Number.isFinite(d) || !Number.isFinite(deltaY) || deltaY === 0) return d;
  return clamp(d * (deltaY > 0 ? DOLLY_STEP : 1 / DOLLY_STEP), minDistance, maxDistance);
}

/**
 * Frame-rate independent glide toward the target (same flavour as
 * OrbitControls' damping, tau ~ 100 ms: ~63 % of the gap closed per tau).
 * The VISUAL scale eases to the wheel's target EVERY frame, so the zoom is
 * continuous; the window only moves at gesture start/end. Snaps when close
 * enough to stop re-framing.
 */
export function approachScale(current, target, dt, tau = 0.1, eps = 0.002) {
  const c = Number(current), t = clampScale(target);
  if (!Number.isFinite(c)) return t;
  const diff = t - c;
  if (Math.abs(diff) <= eps) return t;
  const step = diff * (1 - Math.exp(-Math.max(0, Number(dt) || 0) / tau));
  return Math.abs(t - (c + step)) <= eps ? t : c + step;
}

/** The WINDOW scale that gives a gesture its visual headroom. */
export function headroomWindow(target, headroom = GESTURE_HEADROOM) {
  return clampScale(target) * headroom;
}

/**
 * Gesture state machine (wall-clock ms):
 *  - wheel() on every plain wheel event: whenever the target outgrows the
 *    current window's headroom (which includes gesture START: the fresh
 *    target x headroom is above today's window), it returns the window
 *    scale to grow to; otherwise null — no window move mid-gesture, that
 *    is the whole point.
 *  - tick() every frame: once the wheel has been quiet for idleMs it
 *    returns the final scale to FIT the window to, exactly once per
 *    gesture.
 */
export class GestureTracker {
  constructor(idleMs = GESTURE_IDLE_MS, headroom = GESTURE_HEADROOM, slack = GROW_SLACK) {
    this.idleMs = idleMs;
    this.headroom = headroom;
    this.slack = slack;
    this.active = false;
    this.lastWheel = -Infinity;
  }
  wheel(nowMs, target, windowScale) {
    this.active = true;
    this.lastWheel = nowMs;
    const w = Number(windowScale);
    const cur = Number.isFinite(w) ? Math.max(0, w) : 0;
    // Only grow when the target's headroom no longer fits the window
    // (2% deadband: rounding noise must not churn the compositor). When it
    // does, pad at least +slack so the NEXT few notches won't re-grow too.
    if (headroomWindow(target, this.headroom) <= cur * 1.02) return null;
    return Math.max(headroomWindow(target, this.headroom), cur * this.slack);
  }
  tick(nowMs, target) {
    if (!this.active) return null;
    if (nowMs - this.lastWheel < this.idleMs) return null;
    this.active = false;
    return clampScale(target);
  }
  reset() {
    this.active = false;
    this.lastWheel = -Infinity;
  }
}

/** Visible world half-height at camera distance `dist` for a fov-y camera. */
export function worldHalfHeight(dist, fovDeg) {
  return Math.tan(((Number(fovDeg) || 30) * Math.PI) / 360) * Math.max(0, Number(dist) || 0);
}

/** On-screen pixels per metre at this framing (what the owner sees as size). */
export function pixelsPerMetre(canvasH, dist, fovDeg) {
  return Math.max(1, Number(canvasH) || 1) / (2 * worldHalfHeight(dist, fovDeg));
}

/**
 * Camera distance for visual scale `visualScale` on a canvas of `canvasH`
 * px: pixels-per-metre = visualScale x (the fit-to-body fit at baseH), so
 * her on-screen size depends ONLY on the visual scale and the base window —
 * NEVER on the canvas height. That is what makes the headroom grow and the
 * end-of-gesture fit invisible: re-framing with this distance on any resize
 * preserves pixels-per-metre exactly (see the invariant test).
 */
export function framingDistance(fitDist, canvasH, baseH, visualScale) {
  return (Math.max(0, Number(fitDist) || 0) * Math.max(1, Number(canvasH) || 1))
    / (Math.max(1, Number(baseH) || 1) * clampScale(visualScale));
}

/**
 * Camera look-at height so her feet (world y = yFeet) sit footMarginPx
 * above the canvas bottom. The margin is a CONSTANT PIXEL number, and the
 * window's bottom edge is feet-anchored on screen, so her feet never move
 * across a window resize either.
 */
export function feetAnchorTarget(yFeet, dist, fovDeg, canvasH, footMarginPx = FOOT_MARGIN_PX) {
  const half = worldHalfHeight(dist, fovDeg);
  const f = 1 - (2 * footMarginPx) / Math.max(1, Number(canvasH) || 1);
  return yFeet + half * f;
}

/**
 * THE size-changed rule: a frame whose canvas client size differs from the
 * size the renderer + camera were last framed for MUST be re-framed (and
 * renderer.setSize'd) BEFORE it renders. A compositor resize can land in
 * the same frame it is painted, and the native 'resize' event is not
 * guaranteed to run before the next render — so the render loop compares
 * sizes itself every frame instead of trusting the event. Non-finite or
 * sub-pixel jitter counts as unchanged; anything else re-frames.
 */
export function sizeChanged(curW, curH, appliedW, appliedH) {
  const w = Number(curW), h = Number(curH);
  if (!Number.isFinite(w) || !Number.isFinite(h)) return false;
  const a = Number(appliedW), b = Number(appliedH);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return true;   // never framed yet
  return Math.abs(w - a) > 0.5 || Math.abs(h - b) > 0.5;
}

/**
 * The OrbitControls [minDistance, maxDistance] band that lets THIS frame's
 * framing distance through. controls.update() clamps the camera radius to
 * that band EVERY frame and rewrites camera.position — with the stock
 * 0.5..6 band, a headroom window's framing distance (fitDist x canvas/base,
 * ~8 px at a 2.45x grow) got yanked back to 6 after applyScaleFrame set it,
 * cropping her feet for every frame until the visual glide pulled the true
 * distance back under the cap. The band grows to always contain dist with
 * margin, and never shrinks below the stock defaults, so user dolly stays
 * at least as free as before.
 */
export function controlsDistanceBand(dist, minDistance = 0.5, maxDistance = 6) {
  const d = Number(dist);
  if (!Number.isFinite(d) || d <= 0) return [minDistance, maxDistance];
  return [Math.min(minDistance, d * 0.8), Math.max(maxDistance, d * 1.2)];
}
