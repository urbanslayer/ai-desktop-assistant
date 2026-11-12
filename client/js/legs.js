// legs.js — automatic hop legs for hips_y-driven keyframe animations
// (docs/SELF_ANIMATION.md, "Hops bend the legs" contract). Pure module: no
// three.js, plain [x,y,z] world-space arrays — runs unchanged in the page,
// in `node --test`, and in any harness.
//
// Why: a hop that only keys hips_y rises and lands with STRAIGHT legs. The
// player therefore derives the six leg bones from hips_y every frame (a doc
// that keys a leg bone always wins). Everything is solved in WORLD space
// from the attached rig's rest joint positions — bone local axis signs are
// per-rig (the cookbook in docs/SELF_ANIMATION.md proves the sample avatar
// is axis-mirrored vs the live model), so nothing here encodes an Euler
// convention; the pose layer (pose.js _autoLegs) turns these world targets
// into local rotations by pre-multiplying the REST WORLD quaternion.
//
// Contract behaviour (docs/SELF_ANIMATION.md):
//   hips_y < 0 (crouch / landing): FEET PLANTED — two-bone IK keeps each
//     foot at its rest world position while the hips sink, knees bend
//     toward their rest pole (straight rest legs: pole = character
//     forward, i.e. knees forward). If the dip is deeper than the joint
//     limits allow, the knee bends to its limit and the FOOT SINKS rather
//     than the leg over-rotating.
//   hips_y > 0 (airborne): feet ride with the body PLUS a tuck — knee flex
//     eases smoothly to ~95° at hips_y >= 0.10, thigh forward = 0.3 * knee,
//     the foot rides with the shin and points ~15° beyond it; L/R differ by
//     ~10 % so it does not look mechanical.
//   Both branches agree at hips_y = 0: exact rest (no pop).
//
// Geometric joint-limit clamps mirror the page's KEYFRAME_LIMITS angles
// (leftUpperLeg/rightUpperLeg 103°, leftLowerLeg/rightLowerLeg 126°); the
// pose layer additionally clamps the written local offset into the
// per-axis euler box before applying it.

import { sub, add, mul, dot, len, norm, dist, solveTwoBoneIK } from './ik.js';
import { sample as sampleDoc } from './keyframes.js';

const DEG = Math.PI / 180;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smoothstep = (u) => u * u * (3 - 2 * u);
const UP = [0, 1, 0];
const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];

// ── contract numbers (tune here, not in the caller) ──────────
export const HOP_LEGS = {
  kneeDeg: 102,          // knee flex at full tuck (apex)
  fullY: 0.095,          // hips_y where the tuck is full
  thighFrac: 0.30,      // thigh forward = 0.3 * knee flex
  footPointDeg: 15,     // toe point beyond the shin line at full tuck
  asym: 0.10,           // L/R flex difference (fraction)
  kneeMaxDeg: 126,      // == KEYFRAME_LIMITS.*LowerLeg geometric cap
  thighMaxDeg: 103,     // == KEYFRAME_LIMITS.*UpperLeg geometric cap

  // landing cushion (owner reference video: feet touch down while the body
  // is still DESCENDING, then the knees absorb — head sinks ~9 % H below
  // standing within 0.2 s). A pure function of DOCUMENT time: the player
  // sinks the hips C metres under the doc curve around each touchdown, so
  // the planted IK bends real knees mid-descent instead of waiting for an
  // authored dip key. Tunable here, not in the caller.
  cushion: 0.09,        // C: max extra dip under the doc curve
  cushionFloor: -0.14,  // hipsEff never sinks below this (doc dip + cushion)
  cushionPre: 0.25,     // cushion starts rising this long BEFORE touchdown
  cushionRise: 0.12,    // reaches C this long AFTER touchdown
  cushionHold: 0.45,    // back to exactly 0 this long after touchdown
  riseMin: 0.05,        // airborne segment: peak hips_y >= this
  crossY: 0.01,         // touchdown: curve descends through this hips_y
  cushionHz: 120,       // doc sampling rate for touchdown detection
  cushionSinkPerFrame: 0.004,
                        // max hipsEff SINK per 60 fps frame (m): a knee near
                        // full extension is hypersensitive (acos cliff), so a
                        // 2 cm/frame sink pops >12 deg/frame; the reference
                        // landing takes ~0.2 s to absorb. Rise is uncapped on
                        // purpose (see landingEff).
  kneeRatePerFrame: 3.6, // cap: max tuck-drive CHANGE per 60 fps frame
                        // (world knee angle tracks this at ~1.5x mid-ramp
                        // on twisted rigs, keeping per-frame knee change
                        // under 8 deg)
  tuckAnticip: 0.08,    // anticipation depth (m): the tuck drive counts a
                        // crouch of this depth as progress toward full tuck,
                        // so knees start folding DURING the push-off and full
                        // flex still arrives at the apex under the rate cap
  tuckAnticipLookback: 0.5, // s of doc history scanned for that dip
  pushOffBand: 0.08,   // hips_y (m) band below ground where a rising push-off
                        // releases the foot into the (pre-charged) tuck early:
                        // a planted foot dragged 60 mm up traces a knee-V of
                        // ~30 deg/frame; a heel lift is what a jump does.
  plantFlexPerM: 550,     // planted-branch knee deg per metre of cushioned sink
  tuckWorldFrac: 0.72,     // hinge-flex -> world knee angle factor (avg rig)
  handoverMarginDeg: 6,    // stay in tuck until planted knee is this deeper
  blendRate: 0.125,        // planted<->tuck weight ramp per 120 Hz sample
  landHandoverHi: -0.02,  // eff (m) band where descent hands the leg from
  landHandoverLo: -0.075, // tuck (foot rides) to planted (foot in ground)
  wFadeLo: 0.0,         // hips_y (m) band where the tuck branch hands the
  wFadeHi: 0.12,        // leg back to planted. WIDE on purpose: the descent
                        // runs at 355+ mm/s, so only a 12 cm fade keeps the
                        // branch gap times dw under the knee budget; the
                        // band bottom is continuous because the CUSHION has
                        // already sunk the planted knee to ~45 deg there
  tuckSeamY: 0.12,      // hips_y (m) above ground where the tuck branch has
                        // fully faded in (planted owns everything at or
                        // below 0; knees are hypersensitive in the band)
  kneePushOffFloorDeg: 45,
                        // BELOW the ground line, pre-charge the tuck this
                        // deep when a push-off precedes a hop (the blend
                        // curve releases the foot into it early; it must
                        // not start from zero). ABOVE the line there is NO
                        // floor: the 5.5 deg/frame unwind from the apex
                        // carries ~45 deg of tuck into touchdown by
                        // itself and hands off continuously into the
                        // cushioned planted bend — a floor there snaps the
                        // knee straight-ish then deep again mid-absorb.

  // planted pole: knees track over the toes with a small OUTWARD splay;
  // the rest-knee perpendicular is trusted only when it clearly points
  // forward (a knock-kneed rest rig must not keep bending into an X —
  // owner report: crossed shins in the crouch on the sample avatar).
  poleSplayDeg: 12.5,
  poleFwdDot: 0.7,
};

/** Rotate v by angle (rad) around a UNIT axis (Rodrigues). */
export function rotAxis(v, axis, angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  const k = cross(axis, v);
  const d = dot(axis, v);
  return add(add(mul(v, c), mul(k, s)), mul(axis, d * (1 - c)));
}

/** Horizontal unit forward (falls back to +Z), and the sagittal hinge axis:
 *  normalize(forward x up) points toward the character's RIGHT, so rotating
 *  a downward-pointing bone by a POSITIVE angle about it swings that bone
 *  FORWARD — for any facing, on any rig, with no Euler convention. */
export function hopFrame(forward) {
  const f = forward ? [forward[0], 0, forward[2]] : [0, 0, 1];
  const fl = len(f);
  const fu = fl > 1e-6 ? mul(f, 1 / fl) : [0, 0, 1];
  return { forward: fu, axis: norm(cross(fu, UP)) };
}

/**
 * Touchdown times (seconds, doc time) of the airborne segments of a doc:
 * maximal runs where the sampled hips_y curve is above `crossY` with a peak
 * >= `riseMin`; the touchdown is where the curve DESCENDS through `crossY`
 * after that peak (linear-interpolated between 120 Hz samples). A segment
 * still airborne at the last keyframe has no landing inside the doc and is
 * skipped (the cushion must be exactly 0 at the last keyframe). Pure in the
 * doc: no playback state, so seeking / QA sampling at any t is deterministic.
 */
export function landingTouchdowns(anim, limits = HOP_LEGS) {
  const out = [];
  const dur = Number(anim && anim.duration_s);
  if (!(dur > 0) || !Array.isArray(anim.keyframes) || anim.keyframes.length < 2) return out;
  const N = Math.max(8, Math.ceil(dur * limits.cushionHz));
  const dt = dur / N;
  const ys = new Array(N + 1);
  for (let i = 0; i <= N; i++) ys[i] = sampleDoc(anim, i * dt).hips_y;
  let i = 0;
  while (i <= N) {
    if (!(ys[i] > limits.crossY)) { i++; continue; }
    let j = i;
    while (j <= N && ys[j] > limits.crossY) j++;
    if (j <= N) {                         // run ends inside the doc = a landing
      let pk = i;
      for (let m = i + 1; m < j; m++) if (ys[m] > ys[pk]) pk = m;
      if (ys[pk] >= limits.riseMin) {
        const y0 = ys[j - 1], y1 = ys[j];
        const u = y0 > y1 ? (y0 - limits.crossY) / (y0 - y1) : 0;
        out.push(Math.min((j - 1 + clamp(u, 0, 1)) * dt, dur));
      }
    }
    i = j + 1;
  }
  return out;
}

/**
 * Landing cushion c(t) >= 0 for a doc: the extra metres the hips sink UNDER
 * the doc curve around each touchdown — smooth rise 0 -> C over
 * [t_td - cushionPre, t_td + cushionRise], decay C -> 0 by
 * min(t_td + cushionHold, duration). Exactly 0 at t = 0 and at the last
 * keyframe; docs with no airborne rise -> c == 0 everywhere (behaviour
 * byte-identical). Returns a PURE closure of doc time; overlapping windows
 * take the max.
 */
export function landingCushion(anim, limits = HOP_LEGS) {
  const dur = Number(anim && anim.duration_s) || 0;
  const tds = landingTouchdowns(anim, limits);
  if (!tds.length || !(dur > 0)) return () => 0;
  const win = tds.map((td) => ({
    start: Math.max(0, td - limits.cushionPre),
    mid: Math.min(td + limits.cushionRise, dur),
    end: Math.min(td + limits.cushionHold, dur),
  })).filter((w) => w.end > w.start);
  return (t) => {
    const tt = Number(t);
    if (!Number.isFinite(tt) || tt <= 0 || tt >= dur) return 0;
    let c = 0;
    for (const w of win) {
      if (tt <= w.start || tt >= w.end) continue;
      const up = smoothstep(clamp((tt - w.start) / Math.max(1e-9, w.mid - w.start), 0, 1));
      const hold = tt < w.mid ? 1
        : 1 - smoothstep(clamp((tt - w.mid) / Math.max(1e-9, w.end - w.mid), 0, 1));
      c = Math.max(c, limits.cushion * up * hold);
    }
    return c;
  };
}

/**
 * Effective hips_y curve for the player — PURE function of doc time:
 *   outside landing windows: exactly the doc's hips_y (byte-identical;
 *   docs with no airborne rise are a pass-through).
 *   inside a landing window [t_td - cushionPre, t_td + cushionHold]:
 *   max(hips_y - c(t), cushionFloor) — the body sinks UNDER the doc curve
 *   while descending so the planted IK bends real knees (problem 1) —
 *   then a two-pass rate limit (forward + backward over the 120 Hz grid)
 *   caps the sink at cushionSinkPerFrame per 60 fps frame: a knee near
 *   full extension is hypersensitive, an uncapped 2 cm/frame sink pops
 *   ~15 deg/frame at touchdown. Values outside windows are untouched, so
 *   the crouch key reaches its authored depth exactly.
 */
export function landingEff(anim, limits = HOP_LEGS) {
  const dur = Number(anim && anim.duration_s) || 0;
  const tds = landingTouchdowns(anim, limits);
  if (!tds.length || !(dur > 0)) {
    return (t) => (Number.isFinite(Number(t)) ? sampleDoc(anim, Number(t)).hips_y : 0);
  }
  const cush = landingCushion(anim, limits);
  const N = Math.max(8, Math.ceil(dur * limits.cushionHz));
  const dt = dur / N;
  const step = limits.cushionSinkPerFrame * (60 / limits.cushionHz); // m per sample
  const riseStep = 0.006 * (60 / limits.cushionHz); // m per sample below 0
  const win = tds.map((td) => [td - limits.cushionPre, Math.min(td + limits.cushionHold, dur)]);
  const inWin = (t) => win.some(([a, b]) => t > a && t < b);
  // Rate-limit the CUSHION component itself (rise direction — the cushion
  // deepens — at cushionSinkPerFrame/frame; its decay is already gentle).
  // Capping the whole curve instead would fight the doc's own descent and
  // visibly lag the hop. The cushion arriving ~2 frames late is a feature:
  // it IS the reference-video absorb.
  const doc = new Array(N + 1);
  const cRaw = new Array(N + 1);
  for (let i = 0; i <= N; i++) {
    const t = i * dt;
    doc[i] = sampleDoc(anim, t).hips_y;
    cRaw[i] = inWin(t) ? Math.min(cush(t), doc[i] - limits.cushionFloor) : 0;
  }
  const cEff = new Array(N + 1);
  for (let i = 0; i <= N; i++) {
    cEff[i] = i === 0 ? cRaw[0] : Math.min(cRaw[i], cEff[i - 1] + step);
  }
  for (let i = N - 1; i >= 0; i--) {
    if (cEff[i] > cEff[i + 1] + step) cEff[i] = cEff[i + 1] + step;  // deadline
  }
  const eff = new Array(N + 1);
  eff[0] = doc[0] - cEff[0];
  for (let i = 1; i <= N; i++) {
    const raw = doc[i] - cEff[i];
    // Below the ground line a fast RISE collapses the planted knee at ~1
    // deg/mm (the acos cliff near full extension) — cap it, and the push-off
    // crossing lands inside the 8 deg/frame budget. Above 0 the doc rise is
    // doc-exact: planted IK bends the knee even while hips lift (heel off
    // the ground), so there is no cliff and no lag where it would show.
    eff[i] = (raw >= eff[i - 1] && raw < 0)
      ? Math.min(raw, eff[i - 1] + riseStep) : raw;
  }
  return (t) => {
    const tt = Number(t);
    if (!Number.isFinite(tt) || tt <= 0 || tt >= dur) {
      return tt >= dur ? sampleDoc(anim, dur).hips_y : (tt <= 0 ? sampleDoc(anim, 0).hips_y : 0);
    }
    return eff[clamp(Math.round(tt / dt), 0, N)];
  };
}

/**
 * Tuck-drive curve phi(t) (knee flex, degrees) for a doc — a PURE closure
 * of doc time like the cushion. Raw drive is the height smoothstep of the
 * EFFECTIVE hips_y (landingEff); a two-pass rate limit (forward then
 * backward over the 120 Hz grid) caps knee speed at `kneeRatePerFrame` per
 * 60 fps frame: a doc with a violent push-off (hips_y 0.22 m in 0.3 s)
 * would otherwise swing the knees ~25 deg in one frame mid-ramp. Rate
 * limit only ever lowers phi; docs whose tuck ramp is already gentle
 * enough are untouched (phi == raw).
 */
export function tuckCurve(anim, limits = HOP_LEGS) {
  const dur = Number(anim && anim.duration_s) || 0;
  if (!(dur > 0) || !Array.isArray(anim.keyframes) || anim.keyframes.length < 2) {
    return () => 0;
  }
  const N = Math.max(8, Math.ceil(dur * limits.cushionHz));
  const dt = dur / N;
  const maxStep = limits.kneeRatePerFrame * (60 / limits.cushionHz); // deg per sample
  const lookN = Math.round(limits.tuckAnticipLookback * limits.cushionHz);
  const looping = !!anim.loop;
  const landWin = landingTouchdowns(anim, limits).map(
    (td) => [td - limits.cushionPre, td + limits.cushionHold]);
  // The tuck DRIVE tracks the doc's authored hips_y, never the cushioned
  // eff: the cushion sinks the rig's planted geometry at the ground line,
  // it must not eat the apex knee (or fake a descent) mid-air.
  const y = new Array(N + 1);
  for (let i = 0; i <= N; i++) y[i] = sampleDoc(anim, i * dt).hips_y;
  const wrap = (j) => (looping ? ((j % N) + N) % N : Math.min(N, Math.max(0, j)));
  const phi = new Array(N + 1);
  for (let i = 0; i <= N; i++) {
    // Anticipation needs BOTH a dip in the recent past (energy stored)
    // AND an airborne moment in the near future (somewhere to spend it) —
    // otherwise standing-after-the-last-landing would pre-bend forever.
    const tNow = i * dt;
    let riseAhead = false;
    for (let j = i; j <= i + lookN; j++) {
      if (y[wrap(j)] > 0) { riseAhead = true; break; }
    }
    let nearLanding = false;
    for (const [a, b] of landWin) {
      if (b >= tNow && a <= tNow + limits.tuckAnticipLookback) { nearLanding = true; break; }
    }
    // Below the ground line the floor applies only BEFORE the window opens
    // (push-off ramp-up). After touchdown the planted branch owns the knee
    // and a blended tuck would lift the planted foot off the ground.
    let preLanding = false;
    for (const [a] of landWin) {
      if (a > tNow && a <= tNow + limits.tuckAnticipLookback) { preLanding = true; break; }
    }
    let m = 0;
    for (let j = Math.max(0, i - lookN); j <= i; j++) m = Math.min(m, y[looping ? wrap(j) : j]);
    const pre = clamp(-m, 0, limits.tuckAnticip);
    const drive = limits.kneeDeg * clamp((y[i] + pre) / (limits.fullY + pre), 0, 1);
    if (y[i] > 0) {
      phi[i] = drive;   // no floor above the line (see kneePushOffFloorDeg)
    } else if (preLanding && y[i] <= -0.05) {
      // pre-charge the tuck for the push-off heel-lift, but only from a
      // real crouch: a gentle shallow rise never releases the foot and
      // lets the planted branch ride up smoothly on its own.
      // push-off ramp-up below the ground line: invisible to the rig — the
      // hard branch switch keeps the planted foot exact, and this only
      // pre-charges the tuck so the apex depth arrives inside the flight
      // window under the rate cap.
      phi[i] = Math.max(drive, limits.kneePushOffFloorDeg);
    } else if (riseAhead && pre > 0) {
      phi[i] = drive;
    } else {
      // ground phase after touchdown (or with no hop ahead): planted branch
      // owns the knee; a blended tuck would lift the planted foot
      phi[i] = 0;
    }
  }
  for (let i = 1; i <= N; i++) {
    phi[i] = clamp(phi[i], phi[i - 1] - maxStep, phi[i - 1] + maxStep);
  }
  for (let i = N - 1; i >= 0; i--) {
    phi[i] = clamp(phi[i], phi[i + 1] - maxStep, phi[i + 1] + maxStep);
  }
  return (t) => {
    const tt = Number(t);
    if (!Number.isFinite(tt) || tt <= 0 || tt >= dur) return 0;
    return phi[clamp(Math.round(tt / dt), 0, N)];
  };
}

export function kneeBlendCurve(anim, limits = HOP_LEGS) {
  const dur = Number(anim && anim.duration_s) || 0;
  if (!(dur > 0)) return () => 0;
  const N = Math.max(8, Math.ceil(dur * limits.cushionHz));
  const dt = dur / N;
  const lookN = Math.round(limits.tuckAnticipLookback * limits.cushionHz);
  const looping = !!anim.loop;
  // TUCK keeps the foot riding with the body (airborne mode) — descent
  // included, because during a fast fall a "planted" foot at rest height
  // is fiction: the knee would have to fake a 35 deg mid-air crouch and
  // then snap. The foot hands over to the planted branch exactly when the
  // cushion has carried it INTO the ground (eff below landHandoverLo),
  // where the sinking planted knee already sits at the same ~40-45 deg the
  // unwinding tuck carries in — the cushion is the handover bridge.
  // RISING frames (push-off, standing after the last landing) keep the
  // doc-height fade so the planted foot owns the ground phase.
  const effC = landingEff(anim, limits);
  const y = new Array(N + 1);
  const e = new Array(N + 1);
  for (let i = 0; i <= N; i++) {
    y[i] = sampleDoc(anim, i * dt).hips_y;
    e[i] = effC(i * dt);
  }
  const wrap = (j) => (looping ? ((j % N) + N) % N : Math.min(N, Math.max(0, j)));
  const plantW = (ev) => 1 - smoothstep(clamp((ev - limits.landHandoverHi)
    / (limits.landHandoverLo - limits.landHandoverHi), 0, 1));
  // The tuck branch may only own a frame while it has drive to show for it:
  // with phi ~ 0 the tuck pose IS a straight leg riding off the ground, and
  // handing the final stand-up (shallow sink, phi unwound) to it would snap
  // the knee from ~20 deg to rest in one frame. Gate by phi itself.
  const tuck = tuckCurve(anim, limits);
  const w = new Array(N + 1);
  for (let i = 0; i <= N; i++) {
    const g = clamp((tuck(i * dt) - 2) / 8, 0, 1); // phi gate, see above
    const rising = y[i] > y[Math.max(0, i - 8)] + 0.006;
    if (!rising) { w[i] = plantW(e[i]) * g; continue; }
    const base = y[i] >= limits.wFadeHi ? 1 : y[i] <= limits.wFadeLo ? 0
      : smoothstep(clamp((y[i] - limits.wFadeLo)
        / (limits.wFadeHi - limits.wFadeLo), 0, 1));
    // On rising frames the planted branch only owns the leg down to a much
    // shallower sink than the landing handover band: a push-off crouch that
    // is on its way UP has no business being 60% planted at -43 mm, because
    // the planted knee then free-falls ~30 deg/frame through the ground
    // line before the release catches up. Landing/standing still uses the
    // full handover band below.
    const wr = 1 - smoothstep(clamp((e[i] + 0.045)
      / (limits.landHandoverLo + 0.045), 0, 1));
    w[i] = Math.min(base, wr);
    if (base < 1 && y[i] > -limits.pushOffBand && y[i] < limits.tuckSeamY) {
      let riseAhead = false;
      for (let j = i; j <= i + lookN; j++) {
        if (y[wrap(j)] > 0.01) { riseAhead = true; break; }
      }
      if (riseAhead) {
        // Heel-lift release on a rising push-off (see kneePushOffFloorDeg in
        // tuckCurve for who pre-charges phi). The g gate below already keeps
        // a shallow rise (phi near zero) planted, so no separate readiness
        // factor is needed here.
        w[i] = Math.max(w[i], smoothstep(
          clamp((y[i] + limits.pushOffBand) / limits.pushOffBand, 0, 1)));
      }
    }
    // Above the cushion line the tuck always owns the leg regardless of
    // which way the hips are moving — otherwise the rising-flag detector
    // flickering near the crossing would make w stutter across the branch.
    // Ramps over the first 4 cm so releasing straight at the ground line
    // cannot out-run the knee budget.
    w[i] = Math.max(w[i], clamp(e[i] / 0.04, 0, 1));
    w[i] *= g;
  }
  // Slew-limit the weight itself: the knee angle gap between the two poses
  // can be tens of degrees, so an instantaneous branch flip would pop the
  // world knee even when both poses are individually smooth. 0.08/sample at
  // 120 Hz crosses the full blend in ~0.2 s — invisible for a hop, and it
  // keeps dKnee/dt under the 8 deg/frame budget at every handover. Pure
  // forward pass over the 120 Hz grid: still a pure function of doc time.
  for (let i = 1; i <= N; i++) {
    // Asymmetric: releasing INTO the tuck (plant->tuck, foot leaves the
    // ground) crosses a wide pose gap and must crawl; handing over TO the
    // planted branch at touchdown crosses a nearly-closed gap (the cushion
    // matched the poses) and must be quick so the absorb owns the landing.
    w[i] = clamp(w[i], w[i - 1] - 0.25, w[i - 1] + 0.05);
  }
  return (t) => {
    const tt = Number(t);
    if (!Number.isFinite(tt)) return 0;
    const x = clamp(tt, 0, dur) / dur * N;
    const i = Math.min(N - 1, Math.floor(x));
    return w[i] + (w[i + 1] - w[i]) * (x - i);
  };
}

export function solveHopLeg({ hip, knee, ankle, forward, hipsY, side = null,
                              tuckFlexDeg = null, blendW = null,
                              hipLive = null, sideScale = 1, limits = HOP_LEGS }) {
  if (!hip || !knee || !ankle || !Number.isFinite(hipsY)) return null;
  const l1 = dist(hip, knee), l2 = dist(knee, ankle);
  if (!(l1 > 0.05 && l2 > 0.05)) return null;
  const { forward: f, axis } = hopFrame(forward);
  const u0 = norm(sub(knee, hip));            // rest thigh world direction
  const w0 = norm(sub(ankle, knee));          // rest shin world direction
  const H0 = [hip[0], hip[1] + hipsY, hip[2]]; // hips at this frame's offset
  const H = hipLive ? [hipLive[0], hipLive[1], hipLive[2]] : H0;
  const sway = hipLive ? sub(H, H0) : [0, 0, 0];  // non-hips_y hip offset
  let mode, thighDir, shinDir, kneePos, anklePos, footArc, thighAngle = null, shinAngle = null;

  const doTuck = () => {
    // ── airborne: ride + tuck (feet leave the ground WITH the body) ──
    // Drive: precomputed doc-time curve (rate-limited, with crouch
    // anticipation) when the player supplies it, else the pure height
    // smoothstep (QA / unit default).
    const phi = Number.isFinite(tuckFlexDeg)
      ? clamp(tuckFlexDeg, 0, limits.kneeMaxDeg) * DEG * clamp(sideScale, 0, 2)
      : limits.kneeDeg * DEG * clamp(sideScale, 0, 2)
        * smoothstep(clamp(hipsY / limits.fullY, 0, 1));
    const tf = clamp(phi / (limits.kneeDeg * DEG), 0, 1);
    const th = limits.thighFrac * phi;        // thigh forward
    const gl = th - phi;                      // shin world swing (negative = heel back)
    const tDir = rotAxis(u0, axis, th);
    const sDir = rotAxis(w0, axis, gl);
    const kPos = add(H, mul(tDir, l1));
    // foot rides with the shin and POINTS beyond it (toe drops as the shin
    // swings back; +point curls the toe further down = plantar flex)
    return { mode: 'tuck', thighDir: tDir, shinDir: sDir, kneePos: kPos,
      anklePos: add(kPos, mul(sDir, l2)), thighAngle: th, shinAngle: gl,
      footArc: { axis, angle: gl - limits.footPointDeg * DEG * tf } };
  };

  const doPlanted = () => {
    // ── crouch / landing: feet planted, hips sink, knees forward ──
    // planted target: rest ankle spot, softened toward the PASSIVE foot
    // (sway-following) as hips_y approaches 0 so the branch is exactly
    // continuous there; fully pinned by |hips_y| = 0.01 (a crouch). Long-
    // legged rigs only reach ~49 deg of flex from a pure 8 cm dip — a real
    // crouch also rolls the weight forward (heels lift), so the ankle
    // target leans back 8 mm as the crouch sets in: +2-3 deg of knee flex
    // for an 8 mm xz shift (the contract drift gate is 2 cm).
    const pin = smoothstep(clamp(-hipsY / 0.010, 0, 1));
    const baseT = add(add(ankle, mul(sway, 1 - pin)), mul(f, -0.008 * pin));
    const d = dist(H, baseT);
    // knee flex the geometry demands for hips->planted-ankle distance d
    const cosB = clamp((d * d - l1 * l1 - l2 * l2) / (2 * l1 * l2), -1, 1);
    let beta = Math.acos(cosB);
    // joint limits: the knee cap is direct; the thigh cap bites when the
    // far segment would swing the thigh past its limit (l2 >> l1 rigs)
    let betaMax = limits.kneeMaxDeg * DEG;
    const s = (d * Math.sin(limits.thighMaxDeg * DEG)) / l2;
    if (s < 1) betaMax = Math.min(betaMax, Math.asin(s));
    let target = baseT;
    if (beta > betaMax) {
      // deeper than the legs can fold: bend ONLY to the limit and let the
      // foot sink below its planted rest height (no over-rotation)
      beta = betaMax;
      const dEff = Math.sqrt(Math.max(0, l1 * l1 + l2 * l2 + 2 * l1 * l2 * Math.cos(betaMax)));
      const dir = norm(sub(baseT, H));
      target = add(H, mul(dir, dEff));
    }
    // pole: knees track over the TOES — character forward with a small
    // OUTWARD splay per side. The rest knee's perpendicular around the
    // hip->ankle line is used only when it clearly points FORWARD
    // (dot > poleFwdDot): a knock-kneed rest rig must NOT keep bending
    // its shins into an X (owner report: crossed shins in the crouch).
    // Straight rest legs have no perpendicular and always take the splay.
    const line = norm(sub(baseT, H));
    const ko = sub(knee, hip);
    const perp = sub(ko, mul(line, dot(ko, line)));
    const splay = limits.poleSplayDeg * DEG;
    const outward = side === 'left' ? mul(axis, -1)    // axis = character right
                    : side === 'right' ? axis : [0, 0, 0];
    let pole = norm(add(mul(f, Math.cos(splay)), mul(outward, Math.sin(splay))));
    if (len(perp) > 1e-3 && dot(norm(perp), f) > limits.poleFwdDot) {
      pole = norm(perp);                                // bent-forward rest: keep it
    }
    const sol = solveTwoBoneIK(H, target, l1, l2, pole);
    if (!sol) return null;
    return { mode: 'planted', thighDir: sol.dirUpper, shinDir: sol.dirLower,
      kneePos: sol.elbow, anklePos: sol.wrist, footArc: { axis, angle: 0 } };
  };

  // Cross-fade: the planted branch owns the leg at or below the ground
  // line (the pose consumes directions only — no translation possible —
  // so any tuck share below 0 lifts the planted foot off the ground
  // plane and fails the <=1 cm plant gate). Above 0 the tuck fades in
  // over tuckSeamY, so the crossing is continuous from both sides and
  // the airborne tuck still reaches full depth by hips_y = +seam.
  const seam = limits.tuckSeamY;
  const w = Number.isFinite(blendW) ? clamp(blendW, 0, 1)
    : hipsY >= limits.wFadeHi ? 1 : hipsY <= limits.wFadeLo ? 0
      : smoothstep(clamp((hipsY - limits.wFadeLo)
        / (limits.wFadeHi - limits.wFadeLo), 0, 1));
  const tuckPart = w > 0 ? doTuck() : null;
  const plantedPart = w < 1 ? doPlanted() : null;
  const part = !tuckPart ? plantedPart : !plantedPart ? tuckPart : null;
  if (part) {
    if (!part) return null;
    ({ mode, thighDir, shinDir, kneePos, anklePos, footArc,
      thighAngle, shinAngle } = part);
  } else {
    if (!plantedPart) return null;               // degenerate IK
    mode = w >= 0.5 ? 'tuck' : 'planted';
    thighDir = norm(add(mul(plantedPart.thighDir, 1 - w), mul(tuckPart.thighDir, w)));
    shinDir = norm(add(mul(plantedPart.shinDir, 1 - w), mul(tuckPart.shinDir, w)));
    kneePos = add(H, mul(thighDir, l1));
    anklePos = add(kneePos, mul(shinDir, l2));
    footArc = { axis, angle: plantedPart.footArc.angle * (1 - w) + tuckPart.footArc.angle * w };
    thighAngle = tuckPart.thighAngle * w;
    shinAngle = tuckPart.shinAngle * w;
  }

  const angDeg = (a, b) => Math.acos(clamp(dot(a, b) / (len(a) * len(b)), -1, 1)) / DEG;
  return {
    mode, thighDir, shinDir, kneePos, anklePos, footArc,
    kneeFlexDeg: angDeg(thighDir, shinDir),   // 0 = straight (matches the
    thighFlexDeg: angDeg(u0, thighDir),       // pose.debug legAngles gate)
    thighAngle, shinAngle,                    // tuck-branch hinge angles (rad)
  };
}

/**
 * Both legs of one rig for one frame.
 *   legs: { left: {hip,knee,ankle}, right: {hip,knee,ankle} }  (missing side
 *         -> that side is null; never throws — rigs may lack bones)
 * Returns { left, right } (each a solveHopLeg result or null). Left keeps
 * full tuck, right flexes (1 - asym) of it: the ~10 % L/R difference the
 * contract asks for so a hop does not read mechanical.
 */
export function solveHopLegs({ legs, forward, hipsY, tuckFlexDeg = null,
                               blendW = null, limits = HOP_LEGS }) {
  const out = { left: null, right: null };
  if (!legs) return out;
  const scales = { left: 1, right: 1 - clamp(limits.asym, 0, 0.5) };
  for (const side of ['left', 'right']) {
    const leg = legs[side];
    if (!leg) continue;
    out[side] = solveHopLeg({ ...leg, forward, hipsY, side, tuckFlexDeg, blendW,
                              sideScale: scales[side], limits });
  }
  return out;
}