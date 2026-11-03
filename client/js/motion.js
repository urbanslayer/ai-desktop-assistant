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
//     follow inside the window, look-away for embarrassment; the head (neck
//     bone) follows the gaze slowly
//   - ambient head/neck sway
//   - gesture player: keyframed poses from gestures.js (slerp-eased in/out,
//     arms included), or @pixiv/three-vrm-animation clips when the owner
//     dropped .vrma files into client/animations/
//   - expression blending (emotion tags) + tilt for teasing
//
// The head bone is reserved for the gaze layer; gestures act on the neck
// instead, so nodding never fights looking at you.

import { GESTURES, POSE_BONES, sampleGesture, mirrorPose } from './gestures.js';

const BLEND_IN = 0.35;   // gesture ease-in (s)
const BLEND_OUT = 0.4;   // gesture ease-out (s)
const EASE = (x) => x * x * (3 - 2 * x);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const rand = (lo, hi) => lo + Math.random() * (hi - lo);
// frame-rate independent smoothing factor for exponential approach
const smoothK = (dt, tau) => 1 - Math.exp(-dt / tau);

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

    this._expr = {};          // expressionName -> {current, target}
    this._vowels = {};
    this._vowel = { aa: 0, ih: 0, ee: 0, oh: 0, ou: 0 };
    this._tilt = 0; this._tiltOn = false;

    this.gesture = null;      // {name, loop, t, w, ending, clip?, action?, bones}
    this._ghost = null;
    this.mixer = null;
  }

  // ── persona hot-swap: rebind everything to the new VRM ──
  attach(vrm) {
    this.vrm = vrm;
    this.mixer = null;
    this.gesture = null;
    this._ghost = null;
    this._expr = {};
    this._vowel = { aa: 0, ih: 0, ee: 0, oh: 0, ou: 0 };
    this.vrm1 = vrm.meta.metaVersion !== '0';
    this.armRestZ = this.vrm1 ? 1.3 : -1.3;
    this._boneCache = new Map();
    for (const b of POSE_BONES) {
      const n = vrm.humanoid.getNormalizedBoneNode(b);
      if (n) this._boneCache.set(b, n);
    }
    vrm.lookAt.target = this.eyeTarget;   // eyes follow the fast pivot
    this._resetGaze();
  }

  detach(vrm) {
    if (vrm && vrm.lookAt && vrm.lookAt.target === this.eyeTarget) vrm.lookAt.target = null;
    if (this.vrm === vrm) { this.vrm = null; this.gesture = null; this.mixer = null; }
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

  setVowelTarget(w) { this._vowels = w; }  // from vowelWeights()

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
    let gesturePose = null;
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
      }
      if (g.ending && (g.w <= 0.001 || !d)) this.gesture = null;
    }
    if (this.mixer) this.mixer.update(dt);

    // ── breathing (kept alive under everything) ──
    const breath = Math.sin(this.t * 1.6) * 0.012 * (vrma ? 0.5 : 1);
    if (spine && !(gesturePose && gesturePose.spine !== undefined)) spine.rotation.x += breath;

    // ── gaze: eyes fast, head slow ──
    this._gaze(dt, head, neck, vrma);

    // ── ambient sway (skip the bones a gesture actively owns) ──
    const swayS = vrma ? 0 : 1;
    if (head) {
      head.rotation.z += Math.sin(this.t * 0.7) * 0.03 * swayS;
      head.rotation.y += Math.sin(this.t * 0.45) * 0.05 * swayS;
    }
    if (neck && !(gesturePose && gesturePose.neck !== undefined) && !vrma) {
      neck.rotation.z += Math.sin(this.t * 0.5 + 1.7) * 0.02;
    }

    // tilt (teasing) — additive on top of whatever the neck is doing
    this._tilt += ((this._tiltOn ? 1 : 0) - this._tilt) * smoothK(dt, 0.4);
    if (neck && Math.abs(this._tilt) > 0.001) neck.rotation.z += 0.09 * this._tilt;
  }

  _applyPose(pose, w, bones) {
    const T = this.THREE;
    const q = new T.Quaternion(), e = new T.Euler(), base = new T.Quaternion();
    for (const bone of bones || []) {
      const node = this._node(bone);
      if (!node) continue;
      const rot = pose[bone] || [0, 0, 0];
      e.set(rot[0], rot[1], rot[2]);
      q.setFromEuler(e);
      // blend in/out toward the arms-down rest, not the T-pose zero
      if (bone === 'rightUpperArm' || bone === 'leftUpperArm') {
        base.setFromEuler(new T.Euler(0, 0, bone === 'rightUpperArm' ? this.armRestZ : -this.armRestZ));
      } else base.set(0, 0, 0, 1);
      q.slerp(base, 1 - w);
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

  _gaze(dt, head, neck, vrma) {
    const T = this.THREE;
    const headPos = this._headPos(new T.Vector3());

    // where would she look at right now, before eye jitter?
    let base = new T.Vector3().copy(this.camera.position);
    if (this.gazeMode === 'free' && this._pointer.active && this.t - this._pointer.seenAt < 3) {
      // ray through the pointer, onto the vertical plane through her head
      // facing the camera
      this._ndc.set((this._pointer.x / innerWidth) * 2 - 1, -(this._pointer.y / innerHeight) * 2 + 1);
      this._ray.setFromCamera(this._ndc, this.camera);
      const normal = new T.Vector3().subVectors(headPos, this.camera.position).normalize();
      this._plane.setFromNormalAndCoplanarPoint(normal, headPos);
      if (this._ray.ray.intersectPlane(this._plane, this._pointerPoint)) base = this._pointerPoint.clone();
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

    // eyes follow fast (this is the saccade), the head follows slowly
    this.eyeTarget.position.lerp(gazePoint, smoothK(dt, 0.06));
    this.headPivot.position.lerp(gazePoint, smoothK(dt, 0.45));
    if (this.vrm.lookAt && this.vrm.lookAt.autoUpdate && !vrma) {
      this.vrm.lookAt.target = this.eyeTarget;
    }

    // slow head follow, written on the neck (the head bone belongs to sway)
    if (neck && !vrma) {
      const hp = new T.Vector3().subVectors(this.headPivot.position, headPos);
      const yaw = clamp(Math.atan2(hp.x, hp.z) * 0.5, -0.35, 0.35);
      const pitch = clamp(Math.atan2(hp.y, Math.hypot(hp.x, hp.z)) * 0.4, -0.2, 0.25);
      neck.rotation.y += yaw;
      neck.rotation.x += pitch;
    }
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
    };
  }
}
