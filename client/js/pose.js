// The pose layer: limb-grab IK and the sit pose, driven from the OUTSIDE of
// the life layer. The page calls pose.apply(dt) AFTER motion.update(dt) every
// frame, so pose writes land on top of (and never fight) breath/gaze/gait.
// Item 6 (she makes her own animations) builds on exactly this API:
//   pose.setTarget(id, worldPos) / pose.release(id) / pose.sit(px) /
//   pose.stand() / pose.react(name, opts) / pose.apply(dt, ctx)
//
// Rules honoured (docs/TOUCH.md, docs/BODY.md):
//   - limbs move by their OWN bones (2-bone IK from the live joint chain;
//     head grab rotates the HEAD bone only, within the gaze envelope);
//   - every IK result is clamped to a joint limit vs the rest quaternion,
//     elbows/knees bend only toward their rest-side pole (never hyperextend),
//     and hand/foot targets are pushed OUTSIDE the torso capsule (a capsule
//     along the live spine line, radius from shoulder width);
//   - release eases back over ~0.4 s by un-blending the IK solve toward the
//     rest wrist/ankle, so the last pose frame IS the rest pose (no drift);
//   - sit: hips PINNED to the edge line (closed-loop px feedback while the
//     blend weight is up), thighs roughly forward, shins hanging, spine a
//     little forward, hands left resting (arms at rest are beside the hips).
//
// Needs three.js (passed in, like motion.js) and an attached AvatarMotion
// (for the bone cache, rest snapshots and rig profile — rest quaternions are
// taken from motion, never re-derived, so both layers share one truth).

import { solveTwoBoneIK, sub, add, mul, dot, len, norm } from './ik.js';

const EASE = (x) => x * x * (3 - 2 * x);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smoothK = (dt, tau) => 1 - Math.exp(-dt / tau);

// ids the page can grab -> what they drive
export const GRAB_KIND = {
  hand_l: { chain: 'arm', side: 'left' },
  hand_r: { chain: 'arm', side: 'right' },
  arm_l:  { chain: 'arm', side: 'left' },   // grabbing the forearm drives the wrist
  arm_r:  { chain: 'arm', side: 'right' },
  foot_l: { chain: 'leg', side: 'left' },
  foot_r: { chain: 'leg', side: 'right' },
  head:   { chain: 'head' },
};

// max angle (rad) any pose-driven bone may sit from its REST quaternion —
// the joint-limit safety net on top of the geometric solve (motion.js caps
// gestures at 2.6; grabs must stay strictly inside that).
export const GRAB_LIMITS = {
  leftUpperArm: 2.4, rightUpperArm: 2.4,
  leftLowerArm: 2.2, rightLowerArm: 2.2,
  leftUpperLeg: 1.8, rightUpperLeg: 1.8,
  leftLowerLeg: 2.2, rightLowerLeg: 2.2,
};
// the head grab lives inside the gaze envelope (docs/BODY.md) + a hair of roll
export const HEAD_GRAB_LIMITS = { x: [-0.2, 0.25], y: [-0.4, 0.4], z: [-0.12, 0.12] };

// sit pose (radians, on top of rest, signed by the rig-probed thighSign/kneeSign)
const SIT_THIGH = 1.35;      // thighs roughly horizontal toward the viewer
const SIT_KNEE = 1.30;       // shins hanging down
const SIT_SPINE = 0.10;      // spine slightly forward
const SIT_BLEND = 0.5;       // in/out seconds (docs/TOUCH.md)
const GRAB_IN = 0.18;        // grab pull-in seconds
const RELEASE_OUT = 0.4;     // spring-back seconds (docs/TOUCH.md)

// reaction overlay shapes (instant local touch reactions)
const REACTIONS = {
  headTilt: { dur: 1.2 },    // lean into the pat; opts: {side}
  flinch:   { dur: 0.7 },    // bounce away;              opts: {dir} world-x
  giggle:   { dur: 1.8 },    // wiggle;                   opts: {amp}
};

/** Closest clamped push of point p outside segment capsule a-b radius r. */
export function clampOutsideCapsule(p, a, b, r) {
  const ab = sub(b, a);
  const abLen2 = dot(ab, ab);
  const t = abLen2 > 1e-9 ? clamp(dot(sub(p, a), ab) / abLen2, 0, 1) : 0;
  const near = add(a, mul(ab, t));
  const d = sub(p, near);
  const dl = len(d);
  if (dl >= r) return p;
  if (dl > 1e-6) return add(near, mul(d, r / dl));
  // exactly on the axis: shove along any perpendicular
  return add(near, mul(norm([ab[1], -ab[0], ab[2] || 1e-3]), r));
}

export class PoseLayer {
  /** motion: an attached AvatarMotion (bone cache + _rest + rig). */
  constructor(THREE, motion) {
    this.THREE = THREE;
    this.motion = motion;
    this.vrm = null;
    this.grabs = new Map();     // id -> state
    this.sitState = null;       // {edgePx|edgeWorldY, w, out, offset, blend}
    this.reacts = new Map();    // name -> {t, opts}
    this.flinchX = 0;
    this.torso = { a: [0, 1.0, 0], b: [0, 1.35, 0], r: 0.22 };  // refreshed each frame
    this._legRest = null;       // per-side hip/knee/ankle rest lengths (probed)
    // scratch
    this._q1 = new THREE.Quaternion();
    this._q2 = new THREE.Quaternion();
    this._e1 = new THREE.Euler();
    this._v1 = new THREE.Vector3();
    this._v2 = new THREE.Vector3();
    this._v3 = new THREE.Vector3();
  }

  attach(vrm) {
    this.vrm = vrm;
    this.grabs.clear();
    this.sitState = null;
    this.reacts.clear();
    this.flinchX = 0;
    const T = this.THREE;
    // rest hip->knee->ankle lengths per side (the walk probe only measures the
    // left; grabs need both) + each side's natural knee pole
    this._legRest = {};
    const m = this.motion;
    if (!vrm || !m || !m.rig) return;
    m.vrm.scene.updateMatrixWorld(true);
    for (const side of ['left', 'right']) {
      const hip = m._node(`${side}UpperLeg`);
      const knee = m._node(`${side}LowerLeg`);
      const ankle = m._node(`${side}Foot`);
      if (!hip || !knee || !ankle) continue;
      const h = hip.getWorldPosition(new T.Vector3()).toArray();
      const k = knee.getWorldPosition(new T.Vector3()).toArray();
      const f = ankle.getWorldPosition(new T.Vector3()).toArray();
      const l1 = Math.hypot(...sub(k, h)), l2 = Math.hypot(...sub(f, k));
      if (!(l1 > 0.05 && l2 > 0.05)) continue;
      // pole: the knee's perpendicular offset from the straight hip->ankle
      // line, so the IK branch always bends the knee its own way. Standing
      // legs are straight (perp ~ 0): fall back to the probe's facing sense.
      const dir = norm(sub(f, h));
      let perp = norm(sub(sub(k, h), mul(dir, dot(sub(k, h), dir))));
      if (len(perp) < 1e-3) perp = [0, 0, 0];
      this._legRest[side] = { l1, l2, hip: h, kneePole: perp };
    }
  }

  detach() { this.attach(null); }

  // ── grab API ─────────────────────────────────────────────────
  isGrabbed(id) {
    const g = this.grabs.get(id);
    return !!g && g.state !== 'out';
  }

  grabbedIds() {
    return [...this.grabs.entries()].filter(([, g]) => g.state !== 'out').map(([id]) => id);
  }

  /** Start or steer a grab. worldPos = [x, y, z] (page: pointer ray onto the
   *  plane at the limb's depth). Non-finite input is ignored. */
  setTarget(id, worldPos) {
    if (!GRAB_KIND[id] || !this.vrm) return false;
    const p = Array.isArray(worldPos) ? worldPos
      : worldPos?.toArray ? worldPos.toArray() : null;
    if (!p || !p.every(Number.isFinite)) return false;
    let g = this.grabs.get(id);
    if (!g || g.state === 'out') {
      g = { state: 'in', w: 0, raw: p.slice(), cur: p.slice(), shake: false, outT: 0 };
      this.grabs.set(id, g);
    } else {
      g.raw = p.slice();
    }
    return true;
  }

  /** Let go: eases back to rest over ~0.4 s (shake adds a damped flick). */
  release(id, { shake = false } = {}) {
    const g = this.grabs.get(id);
    if (!g) return;
    g.state = 'out';
    g.outT = 0;
    g.shake = !!shake;
  }

  releaseAll() { for (const id of [...this.grabs.keys()]) this.release(id); }

  // ── sit API ──────────────────────────────────────────────────
  /** sit(edgePx, viewH): pin hips to the window-CSS-px line (desktop sends
   *  sit:<y>). Re-calling moves the line. Pass worldY instead for harnesses
   *  without a window (sitWorld(y)). */
  sit(edgePx, viewH = 0) {
    if (!this.sitState) this.sitState = { edgePx: null, edgeWorldY: null, viewH: 0, w: 0, out: false, offset: 0 };
    const s = this.sitState;
    s.edgePx = Number.isFinite(+edgePx) ? +edgePx : null;
    s.viewH = viewH || s.viewH;
    s.edgeWorldY = null;
    s.out = false;
  }

  sitWorld(worldY) {
    if (!this.sitState) this.sitState = { edgePx: null, edgeWorldY: null, viewH: 0, w: 0, out: false, offset: 0 };
    const s = this.sitState;
    s.edgeWorldY = Number.isFinite(+worldY) ? +worldY : null;
    s.edgePx = null;
    s.out = false;
  }

  stand() {
    if (this.sitState) this.sitState.out = true;
  }

  get sitting() { return !!(this.sitState && !this.sitState.out && this.sitState.w > 0.01); }
  get sitBlendDone() { return !!(this.sitState && !this.sitState.out && this.sitState.w >= 0.999); }
  // once out-blended the sit state is dropped entirely; "done standing" means
  // pose no longer owns ANY sit write (w<=0 covers the final zero-weight frame)
  get standBlendDone() { return !this.sitState || this.sitState.w <= 0; }

  // ── reactions (instant local touch feedback, item 6 reuses these) ──
  react(name, opts = {}) {
    if (!REACTIONS[name]) return false;
    this.reacts.set(name, { t: 0, opts });
    return true;
  }

  /** True while any grab/sit/reaction owns part of the body — the page
   *  freezes idle gestures and roaming while this holds. */
  get active() {
    if (this.grabs.size) return true;
    if (this.sitState && this.sitState.w > 0) return true;
    if (this.reacts.size) return true;
    return false;
  }

  // ── per frame: call AFTER motion.update(dt), BEFORE vrm.update(dt) ──
  apply(dt, ctx = {}) {
    const m = this.motion;
    if (!this.vrm || !m || !m.rig || !m.vrm) return;
    const T = this.THREE;
    this.t = (this.t || 0) + dt;

    // weights first (so this frame already wears them)
    for (const [id, g] of this.grabs) {
      if (g.state === 'in') {
        g.w = Math.min(1, g.w + dt / GRAB_IN);
        if (g.w >= 1) g.state = 'hold';
      } else if (g.state === 'out') {
        g.outT += dt;
        g.w = Math.max(0, 1 - g.outT / RELEASE_OUT);
      }
    }
    // finishing grabs write ONE exact rest/base frame (ew=0) before removal —
    // the last pose frame IS the life-layer pose, so release can never drift
    const finishing = [...this.grabs.entries()]
      .filter(([, g]) => g.state === 'out' && g.w <= 0).map(([id]) => id);
    if (this.sitState) {
      const s = this.sitState;
      const dir = s.out ? -1 : 1;
      s.w = clamp(s.w + dir * dt / SIT_BLEND, 0, s.out ? 1 : 1);
      if (s.out && s.w <= 0) this.sitState = null;
    }
    for (const [name, r] of this.reacts) {
      r.t += dt;
      if (r.t >= REACTIONS[name].dur) this.reacts.delete(name);
    }
    if (!this.grabs.size && !this.sitState && !this.reacts.size) {
      this.flinchX = 0;
      return;
    }

    const scene = m.vrm.scene;
    scene.updateMatrixWorld(true);

    // ── torso capsule from the LIVE spine line ──
    const hipsN = m._node('hips');
    const chestN = m._node('chest') || m._node('upperChest') || m._node('spine');
    if (hipsN && chestN) {
      this.torso.a = hipsN.getWorldPosition(this._v3).toArray();
      this.torso.b = chestN.getWorldPosition(this._v1).toArray();
    }
    this.torso.r = (m.rig.shoulderWidth || 0.3) * 0.42 + (m.rig.headLen || 0.12) * 0.25;

    // ── sit: pose offsets + hips pinned to the edge line ──
    if (this.sitState) this._sitLayer(dt);
    scene.updateMatrixWorld(true);   // grabs read post-sit joint positions

    // ── grabs ──
    for (const [id, g] of this.grabs) {
      const kind = GRAB_KIND[id];
      const ew = EASE(g.w);
      if (kind.chain === 'head') this._grabHead(g, ew);
      else if (kind.chain === 'arm') this._grabArm(id, kind.side, g, ew);
      else this._grabLeg(kind.side, g, ew);
    }
    for (const id of finishing) this.grabs.delete(id);

    // ── reaction overlays on top (head tilt / giggle / flinch) ──
    const headN = m._node('head');
    const spineN = m._node('spine');
    this.flinchX = 0;
    for (const [name, r] of this.reacts) {
      const dur = REACTIONS[name].dur;
      const decay = Math.exp(-3 * (r.t / dur));
      const inRamp = EASE(clamp(r.t / 0.08, 0, 1));
      const w = decay * inRamp;
      if (name === 'headTilt') {
        const side = Math.sign(r.opts.side || 0);
        if (headN) this._mulEuler(headN, 0.10 * w, -0.14 * side * w, -0.18 * side * w);
      } else if (name === 'giggle') {
        const amp = (r.opts.amp ?? 0.07) * w;
        const s = Math.sin(r.t * Math.PI * 2 * 5.5) * amp;
        if (spineN) this._mulEuler(spineN, 0, 0, s);
        if (headN) this._mulEuler(headN, -0.02 * w, 0, -s * 0.6);
      } else if (name === 'flinch') {
        this.flinchX = (r.opts.dir || 1) * 0.035 * Math.cos(r.t * Math.PI * 2 * 4) * w;
      }
    }
    void T;
  }

  // sit legs/spine offsets blend on top of whatever the life layer wrote;
  // hips Y position gets the pin-offset (closed-loop, so hips LAND on the line).
  _sitLayer(dt) {
    const m = this.motion;
    const s = this.sitState;
    const legs = m.rig.legs;
    const e = EASE(s.w);
    const ts = legs ? legs.thighSign : 1;
    const ks = legs ? legs.kneeSign : 1;
    if (legs) {
      for (const side of ['left', 'right']) {
        const up = m._node(`${side}UpperLeg`);
        const lo = m._node(`${side}LowerLeg`);
        if (up) this._mulEuler(up, ts * SIT_THIGH * e, 0, 0);
        if (lo) this._mulEuler(lo, ks * SIT_KNEE * e, 0, 0);
      }
      const spine = m._node('spine');
      if (spine) this._mulEuler(spine, ts * SIT_SPINE * e, 0, 0);
    }
    // pin the hips onto the edge line
    const hips = m._node('hips');
    if (!hips || !m._restPos) return;
    if (s.edgeWorldY != null) {
      const y = hips.getWorldPosition(this._v1).y;
      s.offset = clamp(s.offset + (s.edgeWorldY - (m._restPos.y + s.offset)) * Math.min(1, dt * 12),
                       -1.25 * (legs?.legLen || 0.7), 0.25);
    } else if (s.edgePx != null && ctx_viewH(s)) {
      // px feedback: move hips so their projected y sits on the line
      const { viewH } = { viewH: s.viewH };
      m.vrm.scene.updateMatrixWorld(true);
      const p = hips.getWorldPosition(this._v1).clone().project(m.camera);
      const py = (1 - p.y) / 2 * viewH;
      const dist = Math.max(0.3, m.camera.position.distanceTo(this._v1));
      const mPerPx = (2 * Math.tan((m.camera.fov * Math.PI) / 360) * dist) / Math.max(1, viewH);
      const err = s.edgePx - py;   // px (down = positive)
      s.offset = clamp(s.offset + err * mPerPx * Math.min(1, dt * 15),
                       -1.25 * (legs?.legLen || 0.7), 0.25);
    }
    hips.position.y = m._restPos.y + s.offset * e;
  }

  // ── IK grabs ─────────────────────────────────────────────────
  _armState(side) {
    const arm = this.motion.rig.arms?.[side];
    if (!arm) return null;
    // pole: the rest elbow's perpendicular around the rest arm line — the
    // elbow always bends to its own side, never flips (works VRM0 + VRM1).
    if (!arm._grabPole) {
      const dir = norm(sub(arm.wrist, arm.shoulder));
      const eo = sub(arm.elbow, arm.shoulder);
      let perp = norm(sub(eo, mul(dir, dot(eo, dir))));
      if (len(perp) < 1e-4) perp = norm([dir[2], 0, -dir[0]]);
      arm._grabPole = perp;
    }
    return arm;
  }

  _grabArm(id, side, g, ew) {
    const m = this.motion;
    const T = this.THREE;
    const arm = this._armState(side);
    const upN = m._node(`${side}UpperArm`);
    const loN = m._node(`${side}LowerArm`);
    const handN = m._node(`${side}Hand`);
    if (!arm || !upN || !loN) return;
    const S = upN.getWorldPosition(this._v1);           // live shoulder
    if (!g.home) g.home = arm.wrist.slice();            // spring-back target = rest wrist
    for (let i = 0; i < 3; i++) g.cur[i] += (g.raw[i] - g.cur[i]) * 0.35;
    let target = g.cur.slice();
    if (g.state === 'out') {
      // spring back: the IK target itself eases home
      for (let i = 0; i < 3; i++)      target[i] += (g.home[i] - target[i]) * (1 - ew);
      if (g.shake) {
        const wob = Math.sin(g.outT * Math.PI * 2 * 3.4) * 0.14 * arm.len * ew;
        target[0] += wob; target[2] -= wob * 0.5;
      }
    }
    target = clampOutsideCapsule(target, this.torso.a, this.torso.b,
                                 this.torso.r + 0.02);
    const sol = solveTwoBoneIK(S.toArray(), target, arm.l1, arm.l2, arm._grabPole);
    if (!sol) return;
    // world-space minimal arcs PRE-multiplied by the rest WORLD quaternion
    // (the only correct way on real rigs — see ik.js header), then to local.
    const qU = new T.Quaternion().setFromUnitVectors(
      new T.Vector3().fromArray(arm.restDirUpper), new T.Vector3().fromArray(sol.dirUpper));
    const qL = new T.Quaternion().setFromUnitVectors(
      new T.Vector3().fromArray(arm.restDirLower), new T.Vector3().fromArray(sol.dirLower));
    const qWorldU = qU.multiply(new T.Quaternion().fromArray(arm.restWorldUpper));
    const qWorldL = qL.multiply(new T.Quaternion().fromArray(arm.restWorldLower));
    const parentQ = upN.parent ? upN.parent.getWorldQuaternion(new T.Quaternion()) : new T.Quaternion();
    const localU = parentQ.invert().multiply(qWorldU);
    const localL = qWorldU.clone().invert().multiply(qWorldL);
    const restU = m._rest.get(`${side}UpperArm`), restL = m._rest.get(`${side}LowerArm`);
    upN.quaternion.copy(restU).slerp(this._limit(restU, localU, GRAB_LIMITS[`${side}UpperArm`]), ew);
    loN.quaternion.copy(restL).slerp(this._limit(restL, localL, GRAB_LIMITS[`${side}LowerArm`]), ew);
    if (handN) {
      handN.updateMatrixWorld(true);   // live wrist for the next measure
      g.solved = handN.getWorldPosition(new T.Vector3()).toArray();
    }
  }

  _grabLeg(side, g, ew) {
    const m = this.motion;
    const T = this.THREE;
    const leg = this._legRest?.[side];
    const hipN = m._node(`${side}UpperLeg`);
    const kneeN = m._node(`${side}LowerLeg`);
    const ankleN = m._node(`${side}Foot`);
    if (!leg || !hipN || !kneeN) return;
    const H = hipN.getWorldPosition(this._v1);           // live hip joint
    if (!g.home && ankleN) g.home = ankleN.getWorldPosition(new this.THREE.Vector3()).toArray();
    for (let i = 0; i < 3; i++) g.cur[i] += (g.raw[i] - g.cur[i]) * 0.3;
    let target = g.cur.slice();
    if (g.state === 'out') {
      // ease the IK target back to where the ankle was when we grabbed it
      const home = g.home || target;
      for (let i = 0; i < 3; i++) target[i] += (home[i] - target[i]) * (1 - ew);
    }
    target = clampOutsideCapsule(target, this.torso.a,
                                 [this.torso.b[0], this.torso.b[1] + 0.1, this.torso.b[2]],
                                 this.torso.r * 0.9);
    // knee pole: the rest perpendicular, else "knees forward" (world facing)
    let pole = leg.kneePole;
    if (!pole.every((v) => Math.abs(v) > 1e-3)) {
      pole = new T.Vector3(0, 0, 1)
        .applyQuaternion(m.vrm.scene.getWorldQuaternion(new T.Quaternion())).toArray();
    }
    const sol = solveTwoBoneIK(H.toArray(), target, leg.l1, leg.l2, pole);
    if (!sol) return;
    const restHipW = hipN.getWorldQuaternion(new T.Quaternion());
    const restKneeW = kneeN.getWorldQuaternion(new T.Quaternion());
    // bone geometry directions, read live BEFORE we rotate (same rest-frame
    // trick as _grabArm but against the CURRENT pose, so sit->grab composes):
    const p0 = this._boneDir(hipN, kneeN);               // hip->knee direction
    const p1 = this._boneDir(kneeN, ankleN || kneeN);    // knee->ankle direction
    const qH = new T.Quaternion().setFromUnitVectors(p0, new T.Vector3().fromArray(sol.dirUpper));
    const qK = new T.Quaternion().setFromUnitVectors(p1, new T.Vector3().fromArray(sol.dirLower));
    const qWorldH = qH.clone().multiply(restHipW);
    const qWorldK = qK.clone().multiply(restKneeW);
    const parentQ = hipN.parent.getWorldQuaternion(new T.Quaternion());
    const localH = parentQ.invert().multiply(qWorldH);
    const localK = qWorldH.clone().invert().multiply(qWorldK);
    const restH = m._rest.get(`${side}UpperLeg`), restK = m._rest.get(`${side}LowerLeg`);
    // blend from the CURRENT pose (sit-aware): ew=1 is the absolute IK pose,
    // ew=0 lands exactly back on sit/stand. The hard cap is always vs REST.
    const baseH = hipN.quaternion.clone(), baseK = kneeN.quaternion.clone();
    hipN.quaternion.copy(baseH).slerp(this._limit(restH, localH, GRAB_LIMITS[`${side}UpperLeg`]), ew);
    kneeN.quaternion.copy(baseK).slerp(this._limit(restK, localK, GRAB_LIMITS[`${side}LowerLeg`]), ew);
  }

  _grabHead(g, ew) {
    const m = this.motion;
    const headN = m._node('head');
    if (!headN) return;
    for (let i = 0; i < 3; i++) g.cur[i] += (g.raw[i] - g.cur[i]) * 0.3;
    const hw = headN.getWorldPosition(this._v1);
    const d = this._v2.set(g.cur[0], g.cur[1], g.cur[2]).sub(hw);
    if (d.lengthSq() < 1e-8) return;
    // facing = world +z rotated by her root (matches the gaze math's frame)
    const face = this._v3.set(0, 0, 1)
      .applyQuaternion(m.vrm.scene.getWorldQuaternion(new this.THREE.Quaternion()));
    const fx = face.x, fz = face.z;
    const fl = Math.hypot(fx, fz) || 1;
    const ux = fx / fl, uz = fz / fl;            // unit facing (horizontal)
    const rx = -uz, rz = ux;                     // unit right = facing x up? (left-handed screen sense used by gaze)
    const yaw = Math.atan2(d.x * rx + d.z * rz, d.x * ux + d.z * uz);
    const pitch = Math.atan2(d.y, Math.hypot(d.x, d.z));
    const rest = m._rest.get('head');
    const x = clamp(pitch, HEAD_GRAB_LIMITS.x[0], HEAD_GRAB_LIMITS.x[1]);
    const y = clamp(yaw, HEAD_GRAB_LIMITS.y[0], HEAD_GRAB_LIMITS.y[1]);
    const z = clamp(0.06 * Math.sign(yaw), HEAD_GRAB_LIMITS.z[0], HEAD_GRAB_LIMITS.z[1]);
    this._e1.set(x * ew, y * ew, z * ew);
    this._q1.setFromEuler(this._e1);
    headN.quaternion.copy(rest).multiply(this._q1);
  }

  // ── helpers ──
  _boneDir(fromN, toN) {
    const a = fromN.getWorldPosition(this._v2.clone());
    const b = toN.getWorldPosition(this._v3.clone());
    return b.sub(a).normalize();
  }

  _limit(restQ, qT, maxAng) {
    const a = restQ.angleTo(qT);
    return a > maxAng ? restQ.clone().slerp(qT, maxAng / a) : qT;
  }

  _mulEuler(node, x, y, z) {
    this._e1.set(x, y, z);
    this._q1.setFromEuler(this._e1);
    node.quaternion.multiply(this._q1);
  }

  /** QA/measurement hook (window.rikoMeasure reports these to the harness). */
  debug() {
    const m = this.motion;
    const out = { grabs: {}, sitting: !!this.sitState, sitEdge: null, hipsOffset: null,
                  reacts: [...this.reacts.keys()], flinchX: this.flinchX,
                  torso: { ...this.torso } };
    for (const [id, g] of this.grabs) {
      out.grabs[id] = { state: g.state, w: +g.w.toFixed(3),
                        target: g.cur.map((v) => +v.toFixed(4)), shake: g.shake };
      if (g.solved) out.grabs[id].solved = g.solved.map((v) => +v.toFixed(4));
    }
    if (this.sitState) {
      out.sitEdge = this.sitState.edgePx ?? this.sitState.edgeWorldY;
      out.hipsOffset = +this.sitState.offset.toFixed(4);
      out.sitW = +this.sitState.w.toFixed(3);
      const hips = m._node('hips');
      if (hips) out.hipsWorldY = +hips.getWorldPosition(new this.THREE.Vector3()).y.toFixed(4);
    }
    return out;
  }
}

const ctx_viewH = (s) => (s.edgeWorldY != null) || (Number.isFinite(s.edgePx) && s.viewH > 0);
