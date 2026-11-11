// node --test client/js/keyframes.js — the keyframe player contract
// (docs/SELF_ANIMATION.md): validation, clamps, slerp sampling, the pose
// layer's playKeyframes/stopKeyframes API, and a 10-minute per-frame
// simulation (fixture + 50 random valid animations at 60 fps, interleaved
// with grab/sit interrupts) asserting: no NaN, every bone inside the limits,
// neck never rotated, exact rest within 0.3 s after every end/interrupt, and
// feet planted whenever no leg bone is keyed. Pure-math tests always run;
// the rig tests need three (`npm install` in client/).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  KF_BLEND, KEYFRAME_BONES, KEYFRAME_LIMITS, KEYFRAME_EXPRESSIONS,
  HIPS_Y_MIN, HIPS_Y_MAX, validateAnimation, clampAnimation, sample,
  eulerToQuatXYZ, quatSlerp, quatAngleDeg, easeSmooth, IDENTITY_QUAT,
  clampRelEuler, quatToXyzEuler,
} from './keyframes.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(join(HERE, 'fixtures', 'animation_victory.json'), 'utf8'));

const near = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;
const quatNear = (q, r, eps = 1e-9) =>
  near(Math.abs(q.x * r.x + q.y * r.y + q.z * r.z + q.w * r.w), 1, eps);

// ── pure math (no three needed) ──────────────────────────────

test('the victory fixture is valid and loops cleanly back to rest', () => {
  assert.deepEqual(validateAnimation(FIXTURE), []);
  assert.equal(FIXTURE.name, 'victory_dance');
  const s0 = sample(FIXTURE, 0);
  const sEnd = sample(FIXTURE, FIXTURE.duration_s);
  assert.deepEqual(s0.bones, {});
  assert.deepEqual(sEnd.bones, {});
  assert.equal(sEnd.hips_y, 0);
});

test('easeSmooth is a cubic ease in/out', () => {
  assert.equal(easeSmooth(0), 0);
  assert.equal(easeSmooth(1), 1);
  assert.equal(easeSmooth(0.5), 0.5);
  for (let u = 0; u <= 1; u += 0.05) {
    assert.ok(near(easeSmooth(u) + easeSmooth(1 - u), 1, 1e-12));
    if (u > 0) assert.ok(easeSmooth(u) > easeSmooth(u - 0.05));  // monotone
  }
  assert.ok(easeSmooth(0.001) < 1e-5);   // zero slope at the ends
  assert.ok(1 - easeSmooth(0.999) < 1e-5);
});

test('validation rejects contract violations', () => {
  const base = JSON.parse(JSON.stringify(FIXTURE));
  const bad = (mut, why) => {
    const a = JSON.parse(JSON.stringify(base));
    mut(a);
    assert.ok(validateAnimation(a).length > 0, `should reject: ${why}`);
  };
  bad((a) => { a.name = 'Victory'; }, 'uppercase name');
  bad((a) => { a.name = 'x'; }, 'one-char name');
  bad((a) => { a.duration_s = 9; }, 'duration above 8 s');
  bad((a) => { a.duration_s = 0.3; }, 'duration below 0.4 s');
  bad((a) => { a.ease = 'bounce'; }, 'unknown ease');
  bad((a) => { a.loop = 'yes'; }, 'non-boolean loop');
  bad((a) => { a.keyframes.push({ t: 3.0 }); }, 'last t != duration_s');
  bad((a) => { a.keyframes[0].t = 0.1; }, 'first t != 0');
  bad((a) => { a.keyframes[1].t = 1.5; }, 'non-ascending t');
  bad((a) => {
    a.keyframes[1].bones.neck = [10, 0, 0];
  }, 'neck');
  bad((a) => {
    a.keyframes[1].bones.leftHorn = [0, 0, 0];
  }, 'unknown bone');
  bad((a) => {
    a.keyframes[1].bones.leftUpperArm = [0, 0];
  }, 'two-component bone');
  bad((a) => {
    a.keyframes[1].bones.leftUpperArm = [0, NaN, 0];
  }, 'non-finite bone value');
  bad((a) => { a.keyframes[1].hips_y = 'high'; }, 'non-numeric hips_y');
  bad((a) => { a.keyframes[1].expression.smug = 1; }, 'unknown expression');
  bad((a) => { a.keyframes[1].expression.happy = 1.5; }, 'expression > 1');
  bad((a) => { a.keyframes = [a.keyframes[0]]; }, 'one keyframe');
  assert.deepEqual(validateAnimation(base), []);
});

test('clampAnimation re-clamps angles, hips_y and expressions; drops junk', () => {
  const a = JSON.parse(JSON.stringify(FIXTURE));
  a.keyframes[1].bones.leftUpperArm = [0, 0, 170];    // past ±137
  a.keyframes[1].bones.neck = [30, 0, 0];             // never allowed
  a.keyframes[1].bones.leftHorn = [0, 0, 0];          // unknown
  a.keyframes[2].hips_y = 0.9;                        // past 0.15
  a.keyframes[2].expression.smug = 1;                 // unknown preset
  a.keyframes[2].expression.happy = 1.4;              // past 1
  const { anim, changes } = clampAnimation(a);
  assert.equal(anim.keyframes[1].bones.leftUpperArm[2], 137);
  assert.ok(!('neck' in anim.keyframes[1].bones));
  assert.ok(!('leftHorn' in anim.keyframes[1].bones));
  assert.equal(anim.keyframes[2].hips_y, HIPS_Y_MAX);
  assert.ok(!('smug' in anim.keyframes[2].expression));
  assert.equal(anim.keyframes[2].expression.happy, 1);
  assert.ok(changes.length >= 5);
});

test('sample: slerp between keys with per-segment easing, loops, and rests', () => {
  const a = {
    name: 't', duration_s: 2, ease: 'smooth',
    keyframes: [
      { t: 0, bones: {} },
      { t: 1, bones: { leftUpperArm: [0, 0, 90] }, expression: { happy: 1 }, hips_y: 0.1 },
      { t: 2, bones: {} },
    ],
  };
  assert.deepEqual(sample(a, 0).bones, {});
  const mid = sample(a, 0.5);
  const expectU = easeSmooth(0.5);   // 0.5
  const exp = quatSlerp(IDENTITY_QUAT, eulerToQuatXYZ(0, 0, 90), expectU);
  assert.ok(quatNear(mid.bones.leftUpperArm, exp));
  assert.equal(mid.expression.happy, 0.5);
  assert.ok(near(mid.hips_y, 0.05));
  const atKey = sample(a, 1);
  assert.ok(quatNear(atKey.bones.leftUpperArm, eulerToQuatXYZ(0, 0, 90)));
  assert.equal(atKey.hips_y, 0.1);
  // past the end: clamped (one-shot) vs wrapped (loop)
  assert.ok(quatNear(sample(a, 5).bones.leftUpperArm ?? IDENTITY_QUAT, IDENTITY_QUAT));
  const looped = { ...a, loop: true };
  assert.ok(quatNear(sample(looped, 1 + 2).bones.leftUpperArm,
                     sample(looped, 1).bones.leftUpperArm));
  // linear ease is a plain lerp parameter: u = 0.5 -> happy = 0.5
  const lin = { ...a, ease: 'linear' };
  assert.equal(sample(lin, 0.5).expression.happy, 0.5);
  assert.equal(sample(lin, 0.25).expression.happy, 0.25);
  // an absent bone at both neighbours is never emitted: feet stay planted
  assert.ok(!('leftLowerLeg' in sample(a, 0.5).bones));
});

test('quatAngleDeg measures rotation distance', () => {
  assert.ok(near(quatAngleDeg(IDENTITY_QUAT, eulerToQuatXYZ(0, 0, 90)), 90, 1e-6));
  assert.ok(near(quatAngleDeg(IDENTITY_QUAT, IDENTITY_QUAT), 0));
  // q and -q are the same rotation
  const q = eulerToQuatXYZ(30, -40, 50);
  assert.ok(near(quatAngleDeg(q, { x: -q.x, y: -q.y, z: -q.z, w: -q.w }), 0, 1e-6));
});

test('every limit is finite and symmetric-safe; neck is not a keyframe bone', () => {
  assert.ok(!KEYFRAME_BONES.includes('neck'));
  for (const b of ['spine', 'chest', 'upperChest']) {
    assert.deepEqual(KEYFRAME_LIMITS[b], { x: [-15, 15], y: [-15, 15], z: [-15, 15] });
  }
  assert.deepEqual(KEYFRAME_LIMITS.head, { x: [-40, 40], y: [-40, 40], z: [-40, 40] });
  assert.ok(KEYFRAME_BONES.includes('leftThumbProximal'));
  assert.ok(KEYFRAME_BONES.includes('rightLittleDistal'));
  assert.equal(KEYFRAME_LIMITS.leftUpperLeg.x[1], 103);
  assert.ok(HIPS_Y_MIN === -0.25 && HIPS_Y_MAX === 0.15);
  assert.equal(KF_BLEND, 0.25);
  assert.ok(KEYFRAME_EXPRESSIONS.length === 11);
});

// ── three.js + rig tests ─────────────────────────────────────

let THREE = null;
try { THREE = await import('three'); } catch { /* three not installed */ }
const need = { skip: THREE ? false : 'run `npm install` in client/ (three is a devDependency)' };

if (THREE) {
  const { AvatarMotion } = await import('./motion.js');
  const { PoseLayer } = await import('./pose.js');

  test('eulerToQuatXYZ matches THREE (XYZ order)', need, () => {
    for (const [x, y, z] of [[30, 0, 0], [0, 40, 0], [0, 0, 50], [30, 40, 50],
                             [90, -60, 20], [-45, 80, -10], [137, -126, 103]]) {
      const t = new THREE.Quaternion().setFromEuler(new THREE.Euler(
        x * Math.PI / 180, y * Math.PI / 180, z * Math.PI / 180, 'XYZ'));
      const m = eulerToQuatXYZ(x, y, z);
      assert.ok(quatNear(t, m, 1e-9), `(${x},${y},${z})`);
    }
  });

  test('quatToXyzEuler matches THREE canonical + clampRelEuler keeps >90° hinges',
    need, () => {
      const P = Math.PI;
      for (const [x, y, z] of [[20, -30, 40], [10, 95, 20], [0, 103.6, 0],
                               [137, 137, 0], [-100, 10, 60], [0, -110, 0]]) {
        const q = new THREE.Quaternion().setFromEuler(
          new THREE.Euler(x * P / 180, y * P / 180, z * P / 180, 'XYZ'));
        const mine = quatToXyzEuler(q);
        const three = new THREE.Euler().setFromQuaternion(q, 'XYZ');
        assert.ok(near(mine[0], three.x, 1e-9) && near(mine[1], three.y, 1e-9)
          && near(mine[2], three.z, 1e-9), `decompose (${x},${y},${z})`);
      }
      // an elbow hinge to 110° (canonical reads X/Z=±180!) survives its own
      // clamp untouched — the bug that ate the fixture's 110° keyframes:
      const hinge = eulerToQuatXYZ(0, 110, 0);
      const c = clampRelEuler(hinge, KEYFRAME_LIMITS.leftLowerArm, [0, 110, 0]);
      const back = eulerToQuatXYZ(c[0] / P * 180, c[1] / P * 180, c[2] / P * 180);
      assert.ok(quatNear(hinge, back, 1e-9), '110° hinge clamped away');
      // and an out-of-box spin hint-consistent branch DOES clamp: Y=135 > 126
      const over = eulerToQuatXYZ(0, 135, 0);
      const c2 = clampRelEuler(over, KEYFRAME_LIMITS.leftLowerArm, [0, 135, 0]);
      assert.ok(near(c2[1] * 180 / P, 126, 1e-9), `no clamp: ${c2}`);
    });

  // ── fake rig: pose.test.js's skeleton PLUS finger bones (they are NOT in
  //    the motion cache, so this also drives the _kfBone rest-snapshot path)
  function fakeVRM() {
    const bones = {};
    const mk = (name, parent, x = 0, y = 0, z = 0) => {
      const b = new THREE.Object3D();
      b.name = name;
      b.position.set(x, y, z);
      (parent || scene).add(b);
      bones[name] = b;
      return b;
    };
    const scene = new THREE.Object3D();
    const hips = mk('hips', null, 0, 0.95, 0);
    const spine = mk('spine', hips, 0, 0.1, 0);
    const chest = mk('chest', spine, 0, 0.15, 0);
    const upperChest = mk('upperChest', chest, 0, 0.2, 0);
    const neck = mk('neck', upperChest, 0, 0.15, 0);
    mk('head', neck, 0, 0.12, 0);
    for (const [side, s] of [['left', 1], ['right', -1]]) {
      const sh = mk(`${side}Shoulder`, upperChest, 0.1 * s, 0.12, 0);
      const ua = mk(`${side}UpperArm`, sh, 0.18 * s, -0.02, 0);
      ua.rotation.z = (side === 'right' ? -1.3 : 1.3);   // arms-down rest
      const la = mk(`${side}LowerArm`, ua, 0, -0.28, 0);
      const hand = mk(`${side}Hand`, la, 0, -0.25, 0);
      mk(`${side}IndexProximal`, hand, 0.02, -0.05, 0);   // uncached fingers
      mk(`${side}IndexDistal`, bones[`${side}IndexProximal`], 0, -0.03, 0);
    }
    for (const [side, s] of [['left', 1], ['right', -1]]) {
      const ul = mk(`${side}UpperLeg`, hips, 0.08 * s, -0.05, 0);
      const ll = mk(`${side}LowerLeg`, ul, 0, -0.42, 0);
      mk(`${side}Foot`, ll, 0, -0.40, 0.05);
    }
    mk('eyeLeft', bones.neck, 0.032, 0.05, 0.05);
    mk('eyeRight', bones.neck, -0.032, 0.05, 0.05);
    const expr = new Map();
    return {
      meta: { metaVersion: '1' },
      scene,
      bones,
      humanoid: { getNormalizedBoneNode: (n) => bones[n] || null },
      lookAt: { autoUpdate: true, target: null },
      expressionManager: {
        setValue(name, v) {
          if (!Number.isFinite(v) || v < 0 || v > 1) throw new Error(`bad expression ${name}=${v}`);
          expr.set(name, v);
          return true;
        },
        _values: expr,
      },
    };
  }

  function makeRig() {
    const vrm = fakeVRM();
    const camera = new THREE.PerspectiveCamera(30, 1.6, 0.1, 20);
    camera.position.set(0, 1.55, 2.0);
    camera.lookAt(0, 1.4, 0);
    const motion = new AvatarMotion(THREE, { scene: vrm.scene, camera });
    motion.attach(vrm);
    const pose = new PoseLayer(THREE, motion);
    pose.attach(vrm);
    return { vrm, bones: vrm.bones, motion, pose };
  }

  const sim = (motion, pose, seconds) => {
    for (let i = 0; i < Math.round(seconds * 60); i++) {
      motion.update(1 / 60);
      pose.apply(1 / 60);
    }
  };

  const offsetDeg = (motion, pose, bone) => {
    const nb = pose._kfBone(bone);
    return nb ? nb.rest.angleTo(nb.node.quaternion) * 180 / Math.PI : 0;
  };

  test('playKeyframes: blends in, hits the key pose, stopKeyframes restores rest',
    need, () => {
      const { motion, pose } = makeRig();
      sim(motion, pose, 0.5);
      assert.equal(pose.playKeyframes(FIXTURE, { preview: true }), true);
      assert.equal(pose.isPlaying, true);
      // duplicate action while playing: idempotent, does not restart
      pose.playKeyframes(FIXTURE, { preview: false });
      assert.ok(pose.debug().keyframes.t < 0.1, 'duplicate must not restart t');
      // mid blend-in the pose is only partly there
      sim(motion, pose, 0.1);
      const partial = offsetDeg(motion, pose, 'leftUpperArm');
      sim(motion, pose, 0.45);                      // t ~= 0.55 of key at 0.6
      const risen = offsetDeg(motion, pose, 'leftUpperArm');
      assert.ok(risen > partial && risen > 50,
        `blend-in ${partial.toFixed(1)} -> ${risen.toFixed(1)}`);
      // the sampled offset quaternion must match the pure sample at the SAME
      // internal t (the harness gate in miniature)
      const dbg = pose.debug();
      const want = sample(FIXTURE, dbg.keyframes.t).bones.leftUpperArm;
      const got = dbg.kfBones.leftUpperArm.q;
      assert.ok(quatNear({ x: got[0], y: got[1], z: got[2], w: got[3] }, want, 2e-3),
        `bone ${got} vs sample ${JSON.stringify(want)}`);
      assert.equal(dbg.keyframes.preview, false);   // second action updated it
      assert.ok(dbg.kfBones.leftUpperArm.deg > 50);
      // happy expression tracks the keys
      assert.ok((vrm(pose).expressionManager._values.get('happy') || 0) > 0.3);
      // stop: rest within the 0.3 s contract window, exactly
      pose.stopKeyframes();
      assert.equal(pose.isPlaying, false);
      sim(motion, pose, 0.26);
      assert.equal(pose.debug().keyframes, null);
      for (const b of ['leftUpperArm', 'rightUpperArm', 'leftLowerArm', 'rightLowerArm', 'spine']) {
        assert.equal(offsetDeg(motion, pose, b), 0, `${b} must be exactly rest`);
      }
      assert.equal(vrm(pose).expressionManager._values.get('happy'), 0);
    });
  const vrm = (pose) => pose.vrm;

  test('uncached finger bones get a rest snapshot and return to it', need, () => {
    const { motion, pose } = makeRig();
    const anim = {
      name: 'finger_test', duration_s: 0.8,
      keyframes: [
        { t: 0, bones: {} },
        { t: 0.4, bones: { leftIndexProximal: [0, 80, 0], rightIndexProximal: [0, -80, 0] } },
        { t: 0.8, bones: {} },
      ],
    };
    assert.deepEqual(validateAnimation(anim), []);
    assert.equal(pose.playKeyframes(anim), true);
    sim(motion, pose, 0.4);
    assert.ok(offsetDeg(motion, pose, 'leftIndexProximal') > 60);
    sim(motion, pose, 1.0);
    assert.equal(pose.debug().keyframes, null);
    assert.equal(offsetDeg(motion, pose, 'leftIndexProximal'), 0);
  });

  test('a grab interrupts keyframes and the grab owns the arm', need, () => {
    const { motion, pose } = makeRig();
    pose.playKeyframes(FIXTURE);
    sim(motion, pose, 0.7);
    // the page's wiring: a grab start calls stopKeyframes()
    pose.stopKeyframes();
    pose.setTarget('hand_l', [0.55, 1.75, 0.15]);
    sim(motion, pose, 1.2);                        // kf blended out, grab holds
    assert.equal(pose.debug().keyframes, null);
    assert.ok(pose.isGrabbed('hand_l'));
    pose.release('hand_l');
    sim(motion, pose, 1.0);
    assert.equal(offsetDeg(motion, pose, 'leftUpperArm'), 0);
  });

  // ── the 10-minute simulation ────────────────────────────────
  function mulberry32(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  const SIM_BONES = ['hips', 'spine', 'chest', 'upperChest', 'head',
    'leftUpperArm', 'rightUpperArm', 'leftLowerArm', 'rightLowerArm',
    'leftHand', 'rightHand', 'leftIndexProximal', 'rightMiddleProximal',
    'leftShoulder', 'rightShoulder'];
  const LEG_BONES = ['leftUpperLeg', 'rightUpperLeg', 'leftLowerLeg',
    'rightLowerLeg', 'leftFoot', 'rightFoot'];
  const LIFE_BONES = new Set(['spine', 'head', 'hips']);  // life layer writes these too
  const legsKeyed = (a) => a.keyframes.some((k) => Object.keys(k.bones || {})
    .some((b) => LEG_BONES.includes(b)));

  function randomAnim(rnd, i) {
    const bones = [];
    const pool = rnd() < 0.35 ? [...SIM_BONES, ...LEG_BONES.slice(0, 4)] : SIM_BONES;
    for (const b of pool) if (rnd() < 0.4) bones.push(b);
    if (!bones.length) bones.push('leftUpperArm');
    const dur = +(0.4 + rnd() * 3).toFixed(3);
    const nKeys = 2 + Math.floor(rnd() * 5);
    const keys = [{ t: 0, bones: {}, expression: {} }];
    for (let k = 1; k < nKeys; k++) {
      const key = { t: +((k * dur) / nKeys).toFixed(3), bones: {} };
      for (const b of bones) {
        if (rnd() < 0.65) continue;   // absent at this key -> rest
        const lim = KEYFRAME_LIMITS[b];
        key.bones[b] = ['x', 'y', 'z'].map((ax) =>
          +((rnd() * 2 - 1) * 0.95 * lim[ax][1]).toFixed(2));
      }
      if (rnd() < 0.4) {
        key.expression = {};
        const name = KEYFRAME_EXPRESSIONS[Math.floor(rnd() * KEYFRAME_EXPRESSIONS.length)];
        key.expression[name] = +rnd().toFixed(3);
      }
      if (rnd() < 0.3) key.hips_y = +(HIPS_Y_MIN + rnd() * (HIPS_Y_MAX - HIPS_Y_MIN)).toFixed(3);
      keys.push(key);
    }
    keys.push({ t: dur, bones: {}, expression: {} });
    return {
      name: `sim_anim_${i}`, duration_s: dur, loop: rnd() < 0.35,
      ease: rnd() < 0.5 ? 'smooth' : 'linear', keyframes: keys,
    };
  }

  test('10 simulated minutes at 60 fps: fixture + 50 random animations with '
    + 'grab/sit interrupts (no NaN, limits hold, neck never moves, rest '
    + 'restored within 0.3 s, feet planted without leg keys)', need, () => {
    const { motion, pose } = makeRig();
    const rnd = mulberry32(0xA6C6);
    const anims = [FIXTURE];
    for (let i = 0; i < 50; i++) {
      const a = randomAnim(rnd, i);
      assert.deepEqual(validateAnimation(a), [], `generated anim ${i} invalid`);
      anims.push(a);
    }

    // everything the keyframe layer ever wrote is exactly rest once it and
    // any sit have let go (grabs self-restore; the life layer owns the rest)
    const restCheck = (touched) => {
      for (const b of touched) {
        if (LIFE_BONES.has(b)) continue;
        const nb = pose._kfBone(b);
        if (nb) assert.equal(nb.rest.angleTo(nb.node.quaternion), 0,
          `${b} drifted after the animation ended`);
      }
    };
    const leglessRest = () => {
      for (const b of LEG_BONES) {
        const nb = pose._kfBone(b);
        if (nb) assert.equal(nb.rest.angleTo(nb.node.quaternion), 0,
          `${b} moved although no leg bone is keyed`);
      }
    };

    const DT = 1 / 60;
    const FRAMES = 10 * 60 * 60;              // 10 simulated minutes
    const euler = new THREE.Euler();
    const rel = new THREE.Quaternion();
    const qClamp = new THREE.Quaternion();
    const qOff = new THREE.Quaternion();
    const qExp = new THREE.Quaternion();
    const EASEK = (x) => x * x * (3 - 2 * x);   // pose.js blend easing
    const REST_DEG = 180 / Math.PI;
    let ai = 0, gap = 10, touched = new Set(), pending = null;
    let nextEvent = 200 + Math.floor(rnd() * 300), interrupts = 0, completed = 0;
    let legs = false, grabId = null, sitOn = false, wasPlaying = false;

    for (let f = 0; f < FRAMES; f++) {
      // start the next animation once the body is free (the page's rules)
      if (ai < anims.length && f >= gap && !pose.kf && pose.grabs.size === 0
          && !pose.sitState) {
        touched = new Set();
        legs = legsKeyed(anims[ai]);
        assert.equal(pose.playKeyframes(anims[ai]), true);
        ai += 1;
      }
      // grab/sit interrupts — like the page, they call stopKeyframes()
      if (f >= nextEvent && pose.isPlaying && pending === null) {
        if (rnd() < 0.5 && !grabId) {
          grabId = rnd() < 0.5 ? 'hand_l' : 'foot_r';
          pose.setTarget(grabId, grabId === 'hand_l' ? [0.55, 1.75, 0.15] : [0.2, 0.4, 0.25]);
        } else if (!sitOn) {
          sitOn = true;
          pose.sitWorld(0.55);
        }
        pose.stopKeyframes();
        interrupts += 1;
      }
      if (grabId && f % 4 === 0) {
        pose.setTarget(grabId, grabId === 'hand_l'
          ? [0.55 + 0.1 * Math.sin(f / 30), 1.75, 0.15]
          : [0.2, 0.4 + 0.05 * Math.sin(f / 30), 0.25]);
      }
      if (grabId && f % 240 === 239) { pose.release(grabId); grabId = null; }
      if (sitOn && f % 300 === 299) { pose.stand(); sitOn = false; }
      if (pose.isPlaying && pose.kf.anim.loop
          && pose.kf.t > 1.7 * pose.kf.anim.duration_s) {
        pose.stopKeyframes();          // loop previews end on a stop action
      }

      motion.update(DT);
      pose.apply(DT);

      // per-frame invariants -------------------------------------------------
      for (const [bone, node] of motion._boneCache) {
        const q = node.quaternion;
        assert.ok(Number.isFinite(q.x) && Number.isFinite(q.y)
          && Number.isFinite(q.z) && Number.isFinite(q.w), `NaN in ${bone} f=${f}`);
        if (bone === 'neck') {
          assert.equal(motion._rest.get('neck').angleTo(q), 0, `neck moved f=${f}`);
        }
      }
      if (pose.kf) {
        // what the contract sampler says right now (same t the pose layer uses)
        const kfS = sample(pose.kf.anim, pose.kf.anim.loop ? pose.kf.t
          : Math.min(pose.kf.t, pose.kf.anim.duration_s));
        for (const bone of pose.kf.bones) {
          touched.add(bone);
          const nb = pose._kfBone(bone);
          if (!nb) continue;
          rel.copy(nb.rest).conjugate().multiply(nb.node.quaternion);
          const lim = KEYFRAME_LIMITS[bone];
          if (lim) {
            // inside-the-limits, BRANCH-aware: the offset reads inside the
            // euler box on the branch the sample intends (X/Z=±180 is a
            // legitimate >90° hinge, not a violation — see clampRelEuler)
            const c = clampRelEuler(rel, lim,
              (kfS.deg && kfS.deg[bone]) || [0, 0, 0]);
            qClamp.setFromEuler(euler.set(c[0], c[1], c[2], 'XYZ'));
            assert.ok(rel.angleTo(qClamp) < 1e-6,
              `${bone} offset past its limit box f=${f}`);
          }
          // and it is actually PLAYING the sampled motion, not just legal:
          // (skipped while a grab/sit overrides bones below the kf layer —
          // those frames belong to the interrupt layer, and the clean-rest
          // assertions after the interrupt cover the handover)
          const off = kfS.bones[bone];
          if (off && !pose.kf.ghost && pose.grabs.size === 0
              && pose.sitState === null) {
            qOff.set(off.x, off.y, off.z, off.w);
            qExp.set(0, 0, 0, 1).slerp(qOff, EASEK(pose.kf.w));
            if (lim) {
              // the player's exact pipeline: sample -> blend -> branch-aware
              // clamp (a slerp between two box corners can bulge past an
              // axis; the limits then win over the geodesic — by contract)
              const c2 = clampRelEuler(qExp, lim,
                (kfS.deg && kfS.deg[bone]) || [0, 0, 0]);
              qExp.setFromEuler(euler.set(c2[0], c2[1], c2[2], 'XYZ'));
            }
            const err = rel.angleTo(qExp) * 180 / Math.PI;
            assert.ok(err <= (LIFE_BONES.has(bone) ? 3.0 : 0.5),
              `${bone} drifts ${err.toFixed(2)}° from the sampled pose f=${f} `
              + `grabs=[${[...pose.grabs.keys()]}] sit=${!!pose.sitState} `
              + `anim=${pose.kf.anim.name} t=${pose.kf.t.toFixed(2)} `
              + `w=${pose.kf.w.toFixed(3)} ghost=${!!pose.kf.ghost}`);
          }
        }
        assert.ok(Number.isFinite(motion._node('hips').position.y), 'hips_y NaN');
      }
      if (!legs && pose.sitState === null && pose.grabs.size === 0) leglessRest();

      // blend-out bookkeeping: rest EXACTLY within 0.3 s of every end -------
      const playing = pose.isPlaying;
      if (pending === null && wasPlaying && !playing && pose.kf !== null) {
        pending = { f0: f, touched: new Set(touched) };   // out-blend started
      } else if (pending === null && wasPlaying && !playing) {
        completed += 1;                                    // stopped pre-blend
        gap = f + 20;
      }
      wasPlaying = playing;
      if (pending) {
        if (pose.kf !== null) {
          assert.ok(f - pending.f0 <= 18,
            `keyframes still playing ${((f - pending.f0) / 60).toFixed(2)} s `
            + 'after the end/interrupt (> 0.3 s gate)');
        } else if (pose.grabs.size === 0 && pose.sitState === null) {
          restCheck(pending.touched);   // grab/sit fully released: nothing drifts
          completed += 1;
          pending = null;
          gap = f + 20 + Math.floor(rnd() * 60);
        }
      }
      if (f >= nextEvent) nextEvent = f + 120 + Math.floor(rnd() * 500);
    }

    // final full-body rest (no drift over the whole run)
    pose.releaseAll(); pose.stand();
    for (let i = 0; i < 240; i++) { motion.update(DT); pose.apply(DT); }
    assert.equal(pose.kf, null);
    restCheck(new Set(Object.keys(KEYFRAME_LIMITS)));
    leglessRest();
    assert.ok(ai > 30, `only played ${ai}/${anims.length} animations`);
    assert.ok(interrupts > 10, `only ${interrupts} interrupts happened`);
    assert.ok(completed > 20, `only ${completed} clean ends checked`);
  });
}
