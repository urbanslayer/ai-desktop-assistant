// Plain scroll over her = her SIZE, not the camera (desktop mode): the page
// keeps its fit-to-body framing and asks the desktop to resize the window to
// base_size x scale with her feet anchored (riko/avatar_scale.py does the
// geometry; the page only owns the number). Pure module like walk.js — no
// three.js, no DOM — so `node --test` covers the wheel math.
//
// Ctrl+scroll keeps the OLD behaviour: a camera dolly (close-up crop) for
// those who want it; that math lives here too so both paths are tested.
// Double-click resets the scale to 1.0 (and the page re-fits the camera).

/** The allowed band and the per-notch step (~8%). */
export const SCALE_MIN = 0.4;
export const SCALE_MAX = 2.5;
export const SCALE_STEP = 1.08;
export const DOLLY_STEP = 1.12;   // camera dolly factor per notch (old OrbitControls feel)

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Sanitise a persisted/seeded scale: finite, inside the band, else 1.0. */
export function clampScale(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 1.0;
  return clamp(n, SCALE_MIN, SCALE_MAX);
}

/**
 * One wheel notch on the scale: deltaY < 0 (wheel up / two fingers up)
 * makes her bigger. Repeated calls step by SCALE_STEP and stick at the
 * band edges (scrolling more when clamped stops growing, nothing breaks).
 */
export function stepScale(current, deltaY) {
  const s = clampScale(current);
  if (!Number.isFinite(deltaY) || deltaY === 0) return s;
  return clamp(deltaY < 0 ? s * SCALE_STEP : s / SCALE_STEP, SCALE_MIN, SCALE_MAX);
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
 * OrbitControls' damping): the POSTed scale eases to the wheel's target so
 * the window grows smoothly in small ~2% steps at <= 20 Hz instead of
 * jumping 8% per notch. Snaps when close enough to stop POSTing.
 */
export function approachScale(current, target, dt, tau = 0.1, eps = 0.002) {
  const c = Number(current), t = clampScale(target);
  if (!Number.isFinite(c)) return t;
  const diff = t - c;
  if (Math.abs(diff) <= eps) return t;
  const step = diff * (1 - Math.exp(-Math.max(0, Number(dt) || 0) / tau));
  return Math.abs(t - (c + step)) <= eps ? t : c + step;
}
