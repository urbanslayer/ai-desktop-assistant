// Walking along the bottom strip (docs/WALK_MEMORY.md §W): a pure state
// machine for *when and where* she roams, plus the pure math for the walk
// cycle itself. No three.js, no DOM — runs unchanged in the browser, in
// `node --test`, and in any harness, like director.js / gestures.js / ik.js.
//
// WalkDirector owns the schedule and the strip position x (metres, world
// units around the strip centre):
//   - every `every` seconds while idle she picks a random spot within reach
//     and walks to it for a bounded time;
//   - she never walks while listening / hearing / thinking / speaking /
//     typing, while the approval bubble is up, or while being dragged — and
//     a walk in progress stops (smoothly: the page blends the cycle out) the
//     moment one of those starts;
//   - x is clamped to [bounds.min, bounds.max] (the page derives those from
//     the visible canvas width minus desktop.roam_margin, in world metres);
//     hitting a bound turns her around instead of walking off-screen;
//   - a *forced* gesture (the W shortcut, an asked-for action) stops the
//     walk first — Director handles that policy and calls stopWalk().
//
// Every method takes an explicit `now` in seconds (like Director); tick()
// takes dt so root motion is frame-rate independent:  x += dir * speed * dt,
// speed = base 0.6 m/s scaled by her height and desktop.roam_speed.
//
// Outputs ride the director's action stream:
//   {type:'walk_start', dir, targetX, seconds}  begin walking (dir: +1 =
//       screen-right / +x, -1 = left)
//   {type:'walk_turn', dir}                     mid-walk 180° at a strip edge
//   {type:'walk_stop', reason?}                 stop (arrive/busy/forced/drag)
//
// walkPose() is the gait itself: hips/thighs/shins/feet swing, arms
// counter-swing — all amplitudes derived from THIS rig's leg length (the
// page probes thighSign/kneeSign from the rig at attach time, so VRM 0.x
// rigs whose bone axes differ still bend the right way; see motion.js
// _buildRig legs). Same bone-name -> [x,y,z] euler space as gestures.js.

/** Defaults per docs/WALK_MEMORY.md; the page overrides from settings. */
export const WALK_DEFAULTS = {
  every: [60, 240],       // desktop.roam_every: roam gap seconds [lo, hi]
  walkSeconds: [1, 6],    // one wander lasts 1-6 s
  speed: 0.6,             // m/s at heightRef, scaled by her actual height
  heightRef: 1.5,         // m — 0.6 m/s reference height ("scaled to her height")
  speedMul: 1.0,          // desktop.roam_speed
  marginPx: 40,           // desktop.roam_margin: px kept free at each screen edge
};

// ── The walk cycle ─────────────────────────────────────────────────
// Bone envelope over the WHOLE cycle, relative to rest (radians). The node
// tests assert these hold for every phase; motion.js layers gaze/sway on
// top only for head/neck (which the cycle leaves at rest).
export const WALK_LIMITS = {
  hips: 0.15,             // any axis
  spine: 0.10,
  neck: 0.06,
  leftUpperLeg: 0.55, rightUpperLeg: 0.55,
  leftLowerLeg: 1.40, rightLowerLeg: 1.40,   // knee bend is one-sided, see kneeMax()
  leftFoot: 0.60, rightFoot: 0.60,
  leftUpperArm: 0.50, rightUpperArm: 0.50,
};
export const WALK_BONES = [
  'hips', 'spine', 'neck',
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot',
  'rightUpperLeg', 'rightLowerLeg', 'rightFoot',
  'leftUpperArm', 'rightUpperArm',
];

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Thigh swing amplitude from leg length: stride = 2·legLen·sin(A) ≈ 0.6·legLen. */
export function thighAmp(legLen = 0.7) {
  return clamp(Math.asin(clamp(0.31, 0, 0.95)), 0.18, 0.45);
}

/** Knee flex range (0..this): never hyperextends, never folds past ~1.1 rad. */
export function kneeMax(legLen = 0.7) {
  return clamp(1.0, 0.6, 1.2);
}

/** One full stride in metres for this rig (both steps). */
export function strideLen(cfg) {
  const A = cfg.thighAmp ?? thighAmp(cfg.legLen);
  return 2 * (cfg.legLen || 0.7) * Math.sin(A);
}

/** Gait frequency in cycles/s for a target speed (clamped to a sane range). */
export function cadence(speed, cfg) {
  const s = strideLen(cfg);
  if (!(s > 1e-4)) return 0;
  return clamp(Math.abs(speed) / (2 * s), 0.3, 3.2);
}

/**
 * Sample the gait at cycle phase (radians; one cycle = two steps, left leg
 * forward at phase = pi/2). `cfg`:
 *   { legLen, thighSign, kneeSign, thighAmp?, armSwing? }
 * thighSign/kneeSign come from the rig probe (+1: rotation.x moves the foot
 * forward / bends the knee back; -1: the rig's axis is flipped). Returns
 * { bones: {name:[x,y,z]}, bob } — `bob` is a hips *position* offset in m.
 */
export function walkPose(phase, cfg) {
  const legLen = cfg.legLen > 1e-4 ? cfg.legLen : 0.7;
  const A = cfg.thighAmp ?? thighAmp(legLen);
  const K = kneeMax(legLen);
  const arm = cfg.armSwing ?? Math.min(0.45, 0.9 * A);
  const ts = cfg.thighSign >= 0 ? 1 : -1;
  const ks = cfg.kneeSign >= 0 ? 1 : -1;
  const s = Math.sin(phase), c = Math.cos(phase);
  // right leg forward at sin>0; the left leg mirrors. The knee flexes while
  // its leg swings from back to front (phase-lagged half-sine).
  const thighR = ts * A * s;
  const thighL = -ts * A * s;
  const kneeR = ks * K * Math.max(0, -Math.sin(phase - 0.5));
  const kneeL = ks * K * Math.max(0, Math.sin(phase - 0.5));
  const footR = -0.35 * thighR / (A || 1) * A * 0.8 - 0.25 * kneeR * ks;
  const footL = -0.35 * thighL / (A || 1) * A * 0.8 - 0.25 * kneeL * ks;
  return {
    bones: {
      // hips counter-yaw with the legs, tiny roll, tiny forward drive
      hips: [0.012, 0.135 * A / 0.32 * s, 0.02 * c],
      spine: [0.02 * A / 0.32, -0.048 * A / 0.32 * s, 0],
      neck: [0, -0.05 * A / 0.32 * s, 0],   // head stays roughly on target
      rightUpperLeg: [thighR, 0, 0],
      leftUpperLeg: [thighL, 0, 0],
      rightLowerLeg: [kneeR, 0, 0],
      leftLowerLeg: [kneeL, 0, 0],
      rightFoot: [clamp(footR, -0.5, 0.5), 0, 0],
      leftFoot: [clamp(footL, -0.5, 0.5), 0, 0],
      // arms counter-swing the legs (negative x = forward, gestures.js convention)
      rightUpperArm: [arm * s, 0, 0],
      leftUpperArm: [-arm * s, 0, 0],
    },
    bob: (0.5 * Math.abs(Math.sin(phase)) - 0.25) * 0.10 * legLen * (A / 0.32),
  };
}

/** Max |offset| per axis over the whole cycle for WALK_LIMITS-style checks. */
export function walkPoseEnvelope(cfg) {
  const env = {};
  for (let p = 0; p < Math.PI * 2; p += 0.01) {
    const { bones } = walkPose(p, cfg);
    for (const [b, rot] of Object.entries(bones)) {
      const e = env[b] || (env[b] = 0);
      env[b] = Math.max(e, ...rot.map((v) => Math.abs(v)));
    }
  }
  return env;
}

// ── The roaming schedule ───────────────────────────────────────────
export class WalkDirector {
  constructor({ random = Math.random, ...opts } = {}) {
    this.random = random;
    this.o = { ...WALK_DEFAULTS, ...opts };
    this.enabled = true;
    this.walking = false;
    this.x = 0;             // metres along the strip (world x, strip centre = 0)
    this.dir = 1;           // +1 screen-right, -1 left
    this.targetX = 0;
    this.walkUntil = 0;
    this.speed = 0;         // m/s, recomputed by configure()
    this.nextRoamAt = Infinity;
    this.bounds = { min: -Infinity, max: Infinity };  // set by the page (needs the viewport)
    this.busy = false;      // listening / hearing / thinking / speaking
    this.blocked = { approval: false, typing: false, dragging: false };
  }

  /** Push viewport/size/settings facts the schedule needs. Safe to call often. */
  configure({ every, speedMul, height, bounds, marginWorld } = {}) {
    if (every) this.o.every = every;
    if (Number.isFinite(speedMul)) this.o.speedMul = speedMul;
    if (Number.isFinite(height) && height > 0.2) {
      this.speed = this.o.speed * this.o.speedMul * (height / this.o.heightRef);
    }
    if (bounds) {
      const lo = bounds.min - (Number.isFinite(marginWorld) ? marginWorld : 0);
      const hi = bounds.max + (Number.isFinite(marginWorld) ? marginWorld : 0);
      if (lo < hi) this.bounds = { min: lo, max: hi };
    }
    this.x = clamp(this.x, this.bounds.min, this.bounds.max);
  }

  /** Restore the persisted x at start (clamped once bounds are known). */
  setX(now, x) {
    if (Number.isFinite(x)) this.x = clamp(x, this.bounds.min, this.bounds.max);
    this.arm(now);
  }

  /** Arm the next roam gap (also used after every stop). */
  arm(now) {
    const [lo, hi] = this.o.every;
    this.nextRoamAt = now + lo + this.random() * Math.max(0, hi - lo);
  }

  /** status() passthrough: any non-idle state blocks walking. */
  status(now, state) {
    const busy = state === 'listening' || state === 'hearing'
      || state === 'thinking' || state === 'speaking';
    this.busy = busy;
    if (!busy) return [];           // arming happens in Director.status('idle')
    return this.stopWalk(now, 'busy');
  }

  /** Arm the roam window when she goes back to idle (idempotent: min()). */
  armIdle(now) {
    const [lo, hi] = this.o.every;
    const gap = lo + this.random() * Math.max(0, hi - lo);
    this.nextRoamAt = Math.min(this.nextRoamAt, now + gap);
  }

  /** approval / typing / dragging on|off. Turning any on stops an active walk. */
  setBlocked(now, kind, on) {
    this.blocked[kind] = !!on;
    if (on) return this.stopWalk(now, kind);
    this.armIdle(now);
    return [];
  }

  get idle() {
    return !this.busy && !this.blocked.approval && !this.blocked.typing
      && !this.blocked.dragging;
  }

  /** Stop the walk (smooth: the page blends the cycle out). [] if not walking. */
  stopWalk(now, reason = 'stop') {
    if (!this.walking) return [];
    this.walking = false;
    this.arm(now);
    return [{ type: 'walk_stop', reason }];
  }

  /** QA/test hook: start walking now (capture frames, scripted walks). */
  roamNow(now, { dir = 1, seconds = 4 } = {}) {
    if (this.walking || !this.enabled) return [];
    this.dir = dir >= 0 ? 1 : -1;
    this.targetX = clamp(this.x + this.dir * this.speed * seconds,
                         this.bounds.min, this.bounds.max);
    this.walkUntil = now + seconds;
    this.walking = true;
    return [{ type: 'walk_start', dir: this.dir, targetX: this.targetX, seconds }];
  }

  /**
   * Per frame: integrate root motion and the schedule. `now` seconds, `dt`
   * seconds since the last tick. Returns walk actions for the page.
   */
  tick(now, dt = 1 / 60) {
    const acts = [];
    if (!this.enabled || !Number.isFinite(dt) || dt <= 0 || dt > 1) return acts;
    if (this.walking) {
      if (!this.idle) return acts.concat(this.stopWalk(now, 'busy'));
      this.x += this.dir * this.speed * dt;
      // the strip edges: turn around instead of walking off-screen
      if (this.x <= this.bounds.min && this.dir < 0) {
        this.x = this.bounds.min; this.dir = 1;
        acts.push({ type: 'walk_turn', dir: 1 });
      } else if (this.x >= this.bounds.max && this.dir > 0) {
        this.x = this.bounds.max; this.dir = -1;
        acts.push({ type: 'walk_turn', dir: -1 });
      }
      const arrived = (this.targetX - this.x) * this.dir <= 0;
      if (now >= this.walkUntil || arrived) {
        this.x = clamp(this.targetX, this.bounds.min, this.bounds.max);
        acts.push(...this.stopWalk(now, 'arrive'));
      }
      return acts;
    }
    if (this.idle && now >= this.nextRoamAt && Number.isFinite(this.nextRoamAt)
        && Number.isFinite(this.bounds.min) && Number.isFinite(this.bounds.max)
        && this.speed > 0) {
      const [w0, w1] = this.o.walkSeconds;
      const seconds = w0 + this.random() * (w1 - w0);
      const reach = this.speed * seconds;
      // a spot 25-100 % of one walk-length away, either side, clamped to the strip
      const step = reach * (0.25 + 0.75 * this.random());
      const dir = this.random() < 0.5 ? -1 : 1;
      const target = clamp(this.x + dir * step, this.bounds.min, this.bounds.max);
      const dist = Math.abs(target - this.x);
      if (dist < 0.15) {           // nowhere to go (edge, tiny step): wait and retry
        this.armIdle(now);
        return acts;
      }
      this.dir = dir;
      this.targetX = target;
      this.walkUntil = now + Math.min(seconds, dist / this.speed + 0.5);
      this.walking = true;
      acts.push({ type: 'walk_start', dir: this.dir, targetX: target, seconds });
    }
    return acts;
  }

  snapshot() {
    return { x: this.x, walking: this.walking, dir: this.dir, targetX: this.targetX };
  }
}
