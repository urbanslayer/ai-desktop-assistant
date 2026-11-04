// The life layer: everything that moves her every frame, under the
// director's instructions. Needs three.js (passed in, imported only by the
// page) — the pure parts live in director.js / gestures.js.
//
// Layers mixed here, every frame, on the VRM's *normalized* bones (so a
// persona swap re-targets everything for free):
//   - blink (with occasional double blinks)
//   - breathing (spine)
//   - gaze: eye saccades via vrm.lookAt (small jumps every 1-3 s), big idle
//     glances every 20-40 s, camera lock while listening/speaking, pointer
//     follow inside the window, look-away for embarrassment; the head and
//     neck follow the gaze slowly (neck 40 %, head 60 % of a turn)
//   - ambient head/neck sway (smaller while she is just idling)
//   - gesture player: keyframed poses from gestures.js (slerp-eased in/out,
//     arms included), or @pixiv/three-vrm-animation clips when the owner
//     dropped .vrma files into client/animations/
//   - expression blending (emotion tags) + tilt for teasing
//
// THE ONE RULE: every frame starts from the rest pose. attach() snapshots
// each bone's rest quaternion (captured after the page has lowered the
// arms), update() restores them, and every additive layer composes a
// quaternion *offset* on top (never `bone.rotation.x +=` across frames —
// that accumulated forever and spun her head off her neck). The head-chain
// offsets are hard-clamped per bone (yaw ±0.35, pitch -0.2..0.25,
// roll ±0.12) so no combination of gesture + gaze + sway + tilt can ever
// exceed a believable head turn.
//
// The head bone is reserved for the gaze/sway layers; gestures act on the
// neck instead, so nodding never fights looking at you.

import { GESTURES, POSE_BONES, sampleGesture, mirrorPose } from './gestures.js';

const BLEND_IN = 0.35;   // gesture ease-in (s)
const BLEND_OUT = 0.4;   // gesture ease-out (s)
const EASE = (x) => x * x * (3 - 2 * x);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const rand = (lo, hi) => lo + Math.random() * (hi - lo);
// frame-rate independent smoothing factor for exponential approach
const smoothK = (dt, tau) => 1 - Math.exp(-dt / tau);

// Hard bounds on any head-chain bone's offset from rest (radians). The
// combined head+neck turn stays inside these because the gaze layer splits
// its own ±0.3 rad budget 40 % neck / 60 % head and sway adds only a little.
const HEAD_LIMITS = { x: [-0.2, 0.25], y: [-0.35, 0.35], z: [-0.12, 0.12] };
const GAZE_FOLLOW = { yaw: 0.3, pitch: [-0.18, 0.22] };  // the gaze's own budget
const NECK_SHARE = 0.4;                                    // neck takes 40 % ...
const IDLE_AMBIENT = 0.4;                                  // ... sway shrinks to 40 % while idle

// bones the life layer touches: everything a gesture may touch, plus 'head'
// (POSE_BONES is the gesture vocabulary; the head bone is reserved for the
// gaze/sway layers and must still be cached + rest-snapshotted)
const MOTION_BONES = [...POSE_BONES, 'head'];

// bones each gesture may touch (union over keys), for clean blend-back
const GESTURE_BONES = {};
for (const [name, g] of Object.entries(GESTURES)) {
  const set = new Set();
  for (const k of g.keys) for (const b of Object.keys(k.bones)) set.add(b);
  GESTURE_BONES[name] = [...set];
}

export class AvatarMotion {
  constructor(THREE, { scene, camera }) {
    this.THREE = THREE;
    this.scene = scene;
    this.camera = camera;
    this.vrm = null;
    this.t = 0;

    // gaze objects (created once, live in the scene forever)
    this.eyeTarget = new THREE.Object3D();
    this.headPivot = new THREE.Object3D();
    this._ray = new THREE.Raycaster();
    this._ndc = new THREE.Vector2();
    this._plane = new THREE.Plane();
    scene.add(this.eyeTarget, this.headPivot);

    this.gazeMode = 'free';   // 'free' | 'camera'
    this._pointer = { active: false, x: 0, y: 0, seenAt: -10 };
    this._pointerPoint = new THREE.Vector3();
    this._awayUntil = -1;
    this._awaySide = 1;

    this._nextBlink = rand(2, 6);
    this._blinkT = -1;
    this._doubleBlink = false;
    this._nextSaccade = 0;
    this._saccade = { yaw: 0, pitch: 0 };
    this._nextGlance = rand(20, 40);
    this._glance = { until: 0, yaw: 0, pitch: 0 };
    // smoothed head-follow (the eyes move first; the head catches up slowly)
    this._followYaw = 0;
    this._followPitch = 0;
    this._energy = 1;         // ambient-motion scale: calm while idle, 1 while busy

    this._expr = {};          // expressionName -> {current, target}
    this._vowels = {};
    this._vowel = { aa: 0, ih: 0, ee: 0, oh: 0, ou: 0 };
    this._tilt = 0; this._tiltOn = false;

    this.gesture = null;      // {name, loop, t, w, ending, clip?, action?, bones}
    this._ghost = null;
    this.mixer = null;

    this._rest = new Map();   // bone -> rest quaternion (snapshotted in attach())
    // per-frame scratch (no GC churn at 60 fps)
    this._q1 = new THREE.Quaternion();
    this._q2 = new THREE.Quaternion();
    this._e1 = new THREE.Euler();
    this._v1 = new THREE.Vector3();
    this._v2 = new THREE.Vector3();
  }

  // ── persona hot-swap: rebind everything to the new VRM ──
  attach(vrm) {
    this.vrm = vrm;
    this.mixer = null;
    this.gesture = null;
    this._ghost = null;
    this._expr = {};
    this._vowel = { aa: 0, ih: 0, ee: 0, oh: 0, ou: 0 };
    this._followYaw = 0;
    this._followPitch = 0;
    this.vrm1 = vrm.meta.metaVersion !== '0';
    this._boneCache = new Map();
    this._rest = new Map();
    for (const b of MOTION_BONES) {
      const n = vrm.humanoid.getNormalizedBoneNode(b);
      if (n) {
        this._boneCache.set(b, n);
        // snapshot the rest pose NOW: the page has already lowered the arms
        // to ±1.3 (index.html attach()) before calling us
        this._rest.set(b, n.quaternion.clone());
      }
    }
    vrm.lookAt.target = this.eyeTarget;   // eyes follow the fast pivot
    this._resetGaze();
  }

  detach(vrm) {
    if (vrm && vrm.lookAt && vrm.lookAt.target === this.eyeTarget) vrm.lookAt.target = null;
    if (this.vrm === vrm) {
      this.vrm = null; this.gesture = null; this.mixer = null;
      this._rest = new Map();
    }
  }

  _node(bone) { return this._boneCache.get(bone) || null; }

  _headPos(out) {
    const h = this._node('head') || this._node('neck');
    return h ? h.getWorldPosition(out) : out.set(0, 1.4, 0);
  }

  // ── director / page hooks ───────────────────────────────────
  setGazeMode(mode) { this.gazeMode = mode; }

  setPointer(x, y, active) {
    this._pointer.x = x; this._pointer.y = y;
    this._pointer.active = active;
    if (active) this._pointer.seenAt = this.t;
  }

  lookAway(seconds = 2) {
    this._awayUntil = this.t + seconds;
    this._awaySide = Math.random() < 0.5 ? -1 : 1;
  }

  tilt(on) { this._tiltOn = !!on; }

  /** Blend VRM expressions toward the emotion map's target (null = neutral). */
  applyEmotion(name, value) {
    for (const n of ['happy', 'angry', 'sad', 'relaxed', 'surprised']) {
      this._target(n, n === name ? value : 0);
    }
  }

  setVowelTarget(w) { this._vowel = w; }  // from vowelWeights()

  /**
   * Play a gesture. `clip` (optional) is a THREE.AnimationClip from
   * @pixiv/three-vrm-animation; when present it replaces the built-in pose
   * and owns the bones it animates (we only keep eyes/blinks/breath alive).
   */
  playGesture(name, { clip = null, loop = false } = {}) {
    if (!this.vrm) return;
    const g = GESTURES[name];
    if (!g && !clip) return;
    const same = this.gesture && this.gesture.name === name && !this.gesture.ending;
    if (same) return;
    const old = this.gesture;
    if (old && !old.ending) {
      this._beginGestureBlendOut(old);
      if (!old.clip) { old.t += 0; this._ghost = old; }  // keep easing the old pose out
    }
    this.gesture = {
      name, loop: clip ? loop : (g ? g.loop : false), t: 0, w: 0, ending: false,
      clip, bones: g ? GESTURE_BONES[name] : null, action: null,
    };
    if (clip) {
      this.mixer = this.mixer || new this.THREE.AnimationMixer(this.vrm.scene);
      const action = this.mixer.clipAction(clip);
      action.reset();
      action.setLoop(loop ? this.THREE.LoopRepeat : this.THREE.LoopOnce, loop ? Infinity : 1);
      action.clampWhenFinished = !loop;
      if (old && old.action) action.crossFadeFrom(old.action, BLEND_IN, false);
      else action.fadeIn(BLEND_IN);
      action.play();
      this.gesture.action = action;
    }
  }

  /** Stop a running gesture (loop stop by the director, or one-shot over). */
  stopGesture(name) {
    if (!this.gesture) return;
    if (name && this.gesture.name !== name) return;
    this._beginGestureBlendOut(this.gesture);
  }

  _beginGestureBlendOut(g) {
    g.ending = true;
    if (g.action) {
      g.action.fadeOut(BLEND_OUT);
      const a = g.action;
      setTimeout(() => { a.stop(); a.reset(); }, BLEND_OUT * 1000 + 60);
      if (this.gesture === g) this.gesture = null;   // the mixer keeps fading the clip out
    }
  }

  // ── per frame (call BEFORE vrm.update) ──────────────────────
  update(dt) {
    if (!this.vrm) return;
    this.t += dt;
    const em = this.vrm.expressionManager;
    this._blink(em, dt);
    this._expressions(em, dt);
    this._vowelShapes(em, dt);

    const spine = this._node('spine');
    const neck = this._node('neck');
    const head = this._node('head');

    const vrma = this.gesture && this.gesture.clip;   // VRMA owns the body bones
    // THE reset: every frame starts from the rest pose (the VRMA mixer
    // rewrites the bones it animates every frame anyway, so it is exempt).
    if (!vrma) this._resetPose();

    // how busy is she? calm, small ambient motion while merely idling
    const busy = this.gazeMode !== 'free' || !!this.gesture || !!this._ghost;
    this._energy += ((busy ? 1 : IDLE_AMBIENT) - this._energy) * smoothK(dt, 1.0);

    let gesturePose = null;   // live gesture pose + weight, for the damp below
    let gestureW = 0;
    // a pose gesture that was just replaced keeps easing out (one ghost slot;
    // applied first so the live gesture always wins on shared bones)
    if (this._ghost) {
      const gh = this._ghost;
      const d = GESTURES[gh.name];
      gh.t += dt;
      gh.w = Math.max(0, gh.w - dt / BLEND_OUT);
      if (d && gh.w > 0.001) {
        const t = gh.loop ? gh.t % d.duration : Math.min(gh.t, d.duration);
        this._applyPose(mirrorPose(sampleGesture(gh.name, t), this.vrm1), gh.w, GESTURE_BONES[gh.name]);
        if (GESTURE_BONES[gh.name].includes('neck')) gestureW = gh.w;
      } else this._ghost = null;
    }
    if (this.gesture) {
      const g = this.gesture;
      g.t += dt;
      const d = GESTURES[g.name];
      const dur = d ? d.duration : Infinity;
      // pose weight ramps (one-shots also self-ease via keyframes; loops need this)
      const rampIn = EASE(clamp(g.t / BLEND_IN, 0, 1));
      g.w = g.ending ? g.w : rampIn;
      if (g.ending) g.w = Math.max(0, g.w - dt / BLEND_OUT);
      if (!vrma && d) {
        let t = g.loop ? g.t % dur : Math.min(g.t, dur);
        gesturePose = mirrorPose(sampleGesture(g.name, t), this.vrm1);
        this._applyPose(gesturePose, g.w, GESTURE_BONES[g.name]);
        if (GESTURE_BONES[g.name].includes('neck')) gestureW = Math.max(gestureW, g.w);
      }
      if (g.ending && (g.w <= 0.001 || !d)) this.gesture = null;
    }
    if (this.mixer) this.mixer.update(dt);

    // ── gaze (eyes + the smoothed head-follow target) ──
    const gaze = this._gaze(dt, vrma);

    // breathing (kept alive under everything; gestures own the spine while they run)
    const breath = Math.sin(this.t * 1.6) * 0.012 * (vrma ? 0.5 : 1);
    if (spine && !(gesturePose && gesturePose.spine !== undefined)) {
      this._offsetBone(spine, breath, 0, 0);   // under VRMA the mixer rewrote the bone
    }                                            // this frame, so this can't accumulate

    if (!vrma) {
      // ── additive layers as per-bone euler offsets, composed onto the
      //    rest/gesture quaternion as ONE quaternion each (order-free) ──
      const amb = this._energy;

      // ambient sway
      const swayY = Math.sin(this.t * 0.45) * 0.05 * amb;
      const swayZ = Math.sin(this.t * 0.7) * 0.03 * amb;
      const neckSwayZ = Math.sin(this.t * 0.5 + 1.7) * 0.02 * amb;

      // tilt (teasing), smoothed toward 0/1
      this._tilt += ((this._tiltOn ? 1 : 0) - this._tilt) * smoothK(dt, 0.4);

      // head-follow: neck takes 40 %, head takes 60 % of the turn. While a
      // gesture drives the neck it owns that bone: the neck share fades out
      // and the head keeps only half of its own (a shake is not a lean).
      const neckDamp = 1 - gestureW, headDamp = 1 - 0.5 * gestureW;
      if (neck) {
        this._offsetBone(neck, gaze.pitch * NECK_SHARE * neckDamp,
                               gaze.yaw * NECK_SHARE * neckDamp,
                               neckSwayZ * neckDamp + 0.09 * this._tilt);
      }
      if (head) {
        this._offsetBone(head, gaze.pitch * (1 - NECK_SHARE) * headDamp,
                               gaze.yaw * (1 - NECK_SHARE) * headDamp + swayY,
                               swayZ);
      }

      // the hard guard rail: no layering of gesture + gaze + sway + tilt may
      // leave the believable envelope, relative to rest
      this._boundBone('neck', HEAD_LIMITS);
      this._boundBone('head', HEAD_LIMITS);
    }
  }

  /** Restore every bound bone to its attach()-time rest quaternion. */
  _resetPose() {
    for (const [bone, node] of this._boneCache) {
      const rest = this._rest.get(bone);
      if (rest) node.quaternion.copy(rest);
    }
  }

  /** Compose a small local euler offset on top of what the bone holds now. */
  _offsetBone(node, x, y, z) {
    if (Math.abs(x) < 1e-6 && Math.abs(y) < 1e-6 && Math.abs(z) < 1e-6) return;
    this._e1.set(x, y, z);
    this._q1.setFromEuler(this._e1);
    node.quaternion.multiply(this._q1);   // local frame, on top of rest + gesture
  }

  /** Clamp a bone's total offset from rest to `lim` per axis (XYZ euler). */
  _boundBone(bone, lim) {
    const node = this._node(bone);
    if (!node) return;
    const rest = this._rest.get(bone);
    if (!rest) return;
    this._q1.copy(rest).invert().multiply(node.quaternion);
    this._e1.setFromQuaternion(this._q1, 'XYZ');
    const x = clamp(this._e1.x, lim.x[0], lim.x[1]);
    const y = clamp(this._e1.y, lim.y[0], lim.y[1]);
    const z = clamp(this._e1.z, lim.z[0], lim.z[1]);
    if (x === this._e1.x && y === this._e1.y && z === this._e1.z) return;
    this._q1.setFromEuler(this._e1.set(x, y, z));
    node.quaternion.copy(rest).multiply(this._q1);
  }

  _applyPose(pose, w, bones) {
    const T = this.THREE;
    const q = new T.Quaternion(), e = new T.Euler();
    for (const bone of bones || []) {
      const node = this._node(bone);
      if (!node) continue;
      const rest = this._rest.get(bone);
      if (!rest) continue;
      const rot = pose[bone] || [0, 0, 0];
      e.set(rot[0], rot[1], rot[2]);
      q.setFromEuler(e);
      // blend in/out toward the *snapshot* rest (arms down included), not the T-pose zero
      q.slerp(rest, 1 - w);
      node.quaternion.copy(q);
    }
  }

  // ── blink, with the occasional double ──
  _blink(em, dt) {
    if (this._blinkT < 0 && this.t > this._nextBlink) {
      this._blinkT = 0;
      this._doubleBlink = Math.random() < 0.3;
    }
    if (this._blinkT >= 0) {
      this._blinkT += dt;
      const v = Math.sin(Math.min(this._blinkT / 0.15, 1) * Math.PI);
      em.setValue('blink', v);
      if (this._blinkT > 0.15) {
        if (this._doubleBlink) { this._blinkT = 0.15 - 0.05; this._doubleBlink = false; }
        else { this._blinkT = -1; this._nextBlink = this.t + rand(2, 6); }
      }
    }
  }

  _target(name, value) {
    const e = this._expr[name] || (this._expr[name] = { current: 0, target: 0 });
    e.target = value;
  }

  _expressions(em, dt) {
    const k = smoothK(dt, 0.3);  // blend emotions in and out over the line
    for (const [name, e] of Object.entries(this._expr)) {
      e.current += (e.target - e.current) * k;
      if (e.current < 0.003 && e.target === 0) e.current = 0;
      try { em.setValue(name, e.current); } catch { /* persona without it */ }
    }
  }

  _vowelShapes(em, dt) {
    const w = this._vowels;
    for (const v of ['aa', 'ih', 'ee', 'oh', 'ou']) {
      const want = w[v] || 0;
      const k = smoothK(dt, want > this._vowel[v] ? 0.07 : 0.16);  // fast attack, softer release
      this._vowel[v] += (want - this._vowel[v]) * k;
      try { em.setValue(v, this._vowel[v]); } catch { /* persona without it */ }
    }
  }

  // ── gaze pipeline ───────────────────────────────────────────
  _resetGaze() {
    this.eyeTarget.position.copy(this.camera.position);
    this.headPivot.position.copy(this.camera.position);
  }

  /**
   * Move the eyes (fast, via vrm.lookAt) and return the SMOOTHED head-turn
   * target {yaw, pitch} in the gaze's own budget (|yaw| <= 0.3 rad,
   * pitch in [-0.18, 0.22]); update() splits it neck 40 % / head 60 %.
   */
  _gaze(dt, vrma) {
    const T = this.THREE;
    const headPos = this._headPos(this._v1);

    // where would she look at right now, before eye jitter?
    const base = this._v2.copy(this.camera.position);
    if (this.gazeMode === 'free' && this._pointer.active && this.t - this._pointer.seenAt < 3) {
      // ray through the pointer, onto the vertical plane through her head
      // facing the camera
      this._ndc.set((this._pointer.x / innerWidth) * 2 - 1, -(this._pointer.y / innerHeight) * 2 + 1);
      this._ray.setFromCamera(this._ndc, this.camera);
      const normal = new T.Vector3().subVectors(headPos, this.camera.position).normalize();
      this._plane.setFromNormalAndCoplanarPoint(normal, headPos);
      if (this._ray.ray.intersectPlane(this._plane, this._pointerPoint)) base.copy(this._pointerPoint);
    }

    // small eye saccades every 1-3 s (tiny while locked on the camera)
    if (this.t >= this._nextSaccade) {
      const amp = this.gazeMode === 'camera' ? 0.05 : 0.12;
      this._saccade = { yaw: rand(-amp, amp), pitch: rand(-amp * 0.6, amp * 0.6) };
      this._nextSaccade = this.t + rand(0.8, 2.5);
      if (this.gazeMode === 'free' && !this.gesture) this._nextGlance = Math.min(this._nextGlance, this.t + rand(2, 5));
    }
    // wide idle glances every 20-40 s (off to the side or up, then back)
    if (this.gazeMode === 'free' && this.t >= this._nextGlance) {
      this._glance = {
        until: this.t + rand(1.2, 2.2),
        yaw: (Math.random() < 0.5 ? -1 : 1) * rand(0.5, 0.95),
        pitch: rand(-0.15, 0.35),
      };
      this._nextGlance = this.t + rand(20, 40);
    }
    let gy = this._saccade.yaw, gp = this._saccade.pitch;
    if (this.t < this._glance.until) {
      const remain = Math.min(this._glance.until - this.t, 0.4) / 0.4;  // ease out at the end
      gy += this._glance.yaw * EASE(remain);
      gp += this._glance.pitch * EASE(remain);
    }
    if (this.t < this._awayUntil) {  // embarrassed: look away and down
      const remain = Math.min(this._awayUntil - this.t, 0.5) / 0.5;
      gy += this._awaySide * 0.5 * EASE(remain);
      gp -= 0.25 * EASE(remain);
    }
    if (this.gesture && this.gesture.name === 'look_around') { gy *= 2.2; gp *= 2.2; }

    // rotate the base point around her head by the gaze offsets
    const dir = new T.Vector3().subVectors(base, headPos);
    dir.applyAxisAngle(new T.Vector3(0, 1, 0), gy);
    dir.applyAxisAngle(new T.Vector3(1, 0, 0), gp);
    const gazePoint = new T.Vector3().addVectors(headPos, dir);

    // eyes follow fast (this is the saccade); the smoothed follow state is
    // what the head/neck layers read, so the eyes always move first
    this.eyeTarget.position.lerp(gazePoint, smoothK(dt, 0.06));
    this.headPivot.position.copy(this.eyeTarget.position);
    if (this.vrm.lookAt && this.vrm.lookAt.autoUpdate && !vrma) {
      this.vrm.lookAt.target = this.eyeTarget;
    }

    const hp = new T.Vector3().subVectors(gazePoint, headPos);
    const yawWant = clamp(Math.atan2(hp.x, hp.z) * 0.5, -GAZE_FOLLOW.yaw, GAZE_FOLLOW.yaw);
    const pitchWant = clamp(Math.atan2(hp.y, Math.hypot(hp.x, hp.z)) * 0.4,
                            GAZE_FOLLOW.pitch[0], GAZE_FOLLOW.pitch[1]);
    const k = smoothK(dt, 0.45);   // slow catch-up: no jitter, no overshoot
    this._followYaw += (yawWant - this._followYaw) * k;
    this._followPitch += (pitchWant - this._followPitch) * k;
    return { yaw: this._followYaw, pitch: this._followPitch };
  }

  // ── smoke-test hook ──
  __debug() {
    return {
      gesture: this.gesture ? this.gesture.name : null,
      gestureLoop: this.gesture ? !!this.gesture.loop : false,
      usingClip: !!(this.gesture && this.gesture.clip),
      gazeMode: this.gazeMode,
      blinkT: this._blinkT,
      vowel: { ...this._vowel },
      lookAtSet: !!(this.vrm && this.vrm.lookAt && this.vrm.lookAt.target === this.eyeTarget),
      followYaw: this._followYaw,
      followPitch: this._followPitch,
      energy: this._energy,
    };
  }
}
