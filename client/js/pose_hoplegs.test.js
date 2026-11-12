// node --test — "Hops bend the legs" (docs/SELF_ANIMATION.md) at the pose.js
// level: hips_y-driven keyframe docs must bend the legs automatically on a
// rig whose bones carry NON-IDENTITY rest rotations. That is the point of
// this file: a rig with identity bone frames passes even if the world->local
// conversion forgets to pre-multiply the bone's REST WORLD quaternion, and
// silently does nothing on real VRMs. Here the legs are twisted (Euler X/Y/Z
// rest rotations, mirrored L/R) so any missing rest-world factor lands the
// joints in the wrong place and the planted-foot / tuck assertions fail.
//
// Gates mirrored from the contract: crouch keeps feet within 1 cm of their
// rest world spot (xz <= 2 cm) with the knee bending forward; apex tucks
// (feet clearly airborne); the final frame restores EXACT rest; docs that
// key a leg bone keep that bone's keys untouched; docs without hips_y never
// touch the legs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { landingTouchdowns, landingCushion } from './legs.js';

let THREE = null;
try {
  THREE = await import('three');
} catch { /* three not installed */ }

const need = { skip: THREE ? false : 'run `npm install` in client/ (three is a devDependency)' };

if (THREE) {
  const { AvatarMotion } = await import('./motion.js');
  const { PoseLayer } = await import('./pose.js');

  // ── fake rig whose legs carry non-identity rest rotations ──────────────
  // twist scales the rest-rotation magnitudes: default 1 = violent twist
  // (proves the world->local math), 0.2 = mild natural bend (measures
  // per-frame knee RATE at the ~1:1 ratio real rigs have).
  function twistedVRM(twist = 1) {
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
    hips.rotation.y = Math.PI;                       // VRM-ish hip frame
    const spine = mk('spine', hips, 0, 0.1, 0);
    mk('chest', spine, 0, 0.15, 0);
    for (const [side, s] of [['left', 1], ['right', -1]]) {
      const sh = mk(`${side}Shoulder`, spine, 0.1 * s, 0.12, 0);
      const ua = mk(`${side}UpperArm`, sh, 0.18 * s, -0.02, 0);
      ua.rotation.z = (side === 'right' ? -1.3 : 1.3);
      mk(`${side}LowerArm`, ua, 0, -0.28, 0);
      mk(`${side}Hand`, bones[`${side}LowerArm`], 0, -0.25, 0);
    }
    for (const [side, s] of [['left', 1], ['right', -1]]) {
      const ul = mk(`${side}UpperLeg`, hips, 0.08 * s, -0.05, 0);
      ul.rotation.set(-0.35 * twist, 1.45 * s * twist, 0.12 * s * twist);
      const ll = mk(`${side}LowerLeg`, ul, 0, -0.42, 0);
      ll.rotation.set(0.2 * twist, -1.1 * s * twist, 0.15 * s * twist);
      const ft = mk(`${side}Foot`, ll, 0, -0.40, 0);
      ft.rotation.set(0.8 * twist, 0.2 * s * twist, -0.5 * s * twist);
    }
    mk('eyeLeft', bones.spine, 0.032, 0.25, 0.05);
    mk('eyeRight', bones.spine, -0.032, 0.25, 0.05);
    return {
      meta: { metaVersion: '1' },
      scene,
      bones,
      humanoid: { getNormalizedBoneNode: (n) => bones[n] || null },
      lookAt: { autoUpdate: true, target: null },
      expressionManager: { setValue() {} },
    };
  }

  function makeRig(twist = 1) {
    const vrm = twistedVRM(twist);
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
  const wp = (motion, bone) => {
    motion.vrm.scene.updateMatrixWorld(true);
    return motion._node(bone).getWorldPosition(new THREE.Vector3());
  };

  // hop recipe per docs/SELF_ANIMATION.md: crouch, apex, touchdown, land
  const HOP = {
    name: 'recipe_hop', duration_s: 1.5, loop: false, ease: 'smooth',
    keyframes: [
      { t: 0.0, bones: {}, expression: {} },
      { t: 0.2, bones: {}, expression: {}, hips_y: -0.08 },
      { t: 0.3, bones: {}, expression: {}, hips_y: -0.08 },
      { t: 0.6, bones: {}, expression: {}, hips_y: 0.14 },
      { t: 0.9, bones: {}, expression: {}, hips_y: 0.0 },
      { t: 1.1, bones: {}, expression: {}, hips_y: -0.05 },
      { t: 1.5, bones: {}, expression: {} },
    ],
  };

  // drive one exact doc time: apply() advances kf.t by dt BEFORE sampling,
  // so pin t = target - dt and one apply lands exactly on the target with
  // the blend fully open (w pinned to 1).
  function at(motion, pose, t) {
    const dt = 1 / 60;
    pose.kf.w = 1;
    pose.kf.t = t - dt;
    motion.update(dt);
    pose.apply(dt);
  }

  const legMetrics = (motion) => {
    const out = {};
    for (const side of ['left', 'right']) {
      const h = wp(motion, `${side}UpperLeg`);
      const k = wp(motion, `${side}LowerLeg`);
      const a = wp(motion, `${side}Foot`);
      const th = k.clone().sub(h), sh = a.clone().sub(k);
      out[side] = {
        kneeFlexDeg: th.angleTo(sh) * 180 / Math.PI,
        foot: a,
        kneeZ: k.z,
        kneeX: k.x,
      };
    }
    return out;
  };

  test('fake rig sanity: twisted legs, non-identity rest world quats', need, () => {
    const { motion } = makeRig();
    motion.vrm.scene.updateMatrixWorld(true);
    for (const side of ['left', 'right']) {
      const q = motion._node(`${side}UpperLeg`).getWorldQuaternion(new THREE.Quaternion());
      assert.ok(Math.abs(q.w) < 0.999, `${side}UpperLeg rest world quat is ~identity`);
      const r = motion._rest.get(`${side}UpperLeg`);
      assert.ok(r.angleTo(new THREE.Quaternion()) > 1.0, 'rest LOCAL must be twisted');
    }
  });

  test('crouch key: feet planted (<=1 cm / <=2 cm xz), knees forward', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const rest = legMetrics(motion);
    assert.equal(pose.playKeyframes(HOP), true);
    at(motion, pose, 0.25);         // inside the -0.08 crouch hold
    const m = legMetrics(motion);
    const dbg = pose.debug();
    // planted feet are LEFT FLAT (foot bones untouched) -> only 4 writes
    assert.deepEqual([...dbg.autoLegs].sort(),
      ['leftLowerLeg', 'leftUpperLeg', 'rightLowerLeg', 'rightUpperLeg']);
    assert.equal(dbg.autoLegSolve.left.mode, 'planted');
    for (const side of ['left', 'right']) {
      const dy = Math.abs(m[side].foot.y - rest[side].foot.y);
      const xz = Math.hypot(m[side].foot.x - rest[side].foot.x,
        m[side].foot.z - rest[side].foot.z);
      assert.ok(dy <= 0.01, `${side} foot sank ${dy.toFixed(4)} m (gate 0.01)`);
      assert.ok(xz <= 0.02, `${side} foot drifted ${xz.toFixed(4)} m (gate 0.02)`);
      assert.ok(m[side].kneeFlexDeg > rest[side].kneeFlexDeg + 25,
        `${side} knee did not deepen (${rest[side].kneeFlexDeg} -> ${m[side].kneeFlexDeg})`);
      // hips actually sank by the keyed amount
      assert.ok(Math.abs(motion._node('hips').position.y
        - motion._restPos.y + 0.08) < 1e-3, 'hips_y not applied');
    }
  });

  test('apex key: full tuck, feet clearly airborne, L/R differ ~10 %', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const rest = legMetrics(motion);
    pose.playKeyframes(HOP);
    at(motion, pose, 0.6);
    const m = legMetrics(motion);
    const dbg = pose.debug();
    assert.equal(dbg.autoLegSolve.left.mode, 'tuck');
    for (const side of ['left', 'right']) {
      const rise = m[side].foot.y - rest[side].foot.y;
      assert.ok(rise > 0.08, `${side} foot not airborne (rise ${rise.toFixed(3)} m)`);
      assert.ok(m[side].kneeFlexDeg > rest[side].kneeFlexDeg + 60,
        `${side} knee flex only ${m[side].kneeFlexDeg} deg at apex`);
    }
    const ratio = m.right.kneeFlexDeg / m.left.kneeFlexDeg;
    assert.ok(ratio > 0.8 && ratio < 0.98, `L/R tuck ratio ${ratio.toFixed(3)}`);
  });

  test('landing key: knees absorb (deeper than rest), feet back planted', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const rest = legMetrics(motion);
    pose.playKeyframes(HOP);
    at(motion, pose, 1.1);
    const m = legMetrics(motion);
    assert.equal(pose.debug().autoLegSolve.left.mode, 'planted');
    for (const side of ['left', 'right']) {
      const dy = Math.abs(m[side].foot.y - rest[side].foot.y);
      assert.ok(dy <= 0.01, `${side} foot ${dy.toFixed(4)} m off rest at landing`);
      assert.ok(m[side].kneeFlexDeg > rest[side].kneeFlexDeg + 15,
        `${side} landing knee too straight (${m[side].kneeFlexDeg})`);
    }
  });

  test('continuity: one frame before/after hips_y crosses 0 the legs barely move', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    pose.playKeyframes(HOP);
    at(motion, pose, 0.4167);                  // push-off: hips_y crosses 0 at ~0.423
    const a = legMetrics(motion).left.foot.clone();
    at(motion, pose, 0.4333);
    const b = legMetrics(motion).left.foot.clone();
    assert.ok(a.distanceTo(b) < 0.15, `foot jumped ${a.distanceTo(b).toFixed(3)} m`);
  });

  test('final frame restores EXACT rest for every auto-leg bone', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    pose.playKeyframes(HOP);
    sim(motion, pose, 1.4);                    // past t=duration into blend-out
    sim(motion, pose, 0.6);                    // blend-out completes
    assert.equal(pose.debug().keyframes, null);
    for (const b of ['leftUpperLeg', 'leftLowerLeg', 'leftFoot',
                     'rightUpperLeg', 'rightLowerLeg', 'rightFoot']) {
      const nb = pose._kfBone(b);
      assert.equal(nb.rest.angleTo(nb.node.quaternion), 0, `${b} not exact rest`);
    }
    assert.equal(motion._node('hips').position.y, motion._restPos.y);
  });

  test('keyed leg bones win; unkeyed ones still derive (per-bone gate)', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const rest = legMetrics(motion);
    const keyed = JSON.parse(JSON.stringify(HOP));
    keyed.name = 'keyed_left_thigh';
    for (const k of keyed.keyframes) {
      k.bones = k.bones || {};
      k.bones.leftUpperLeg = [20, 0, 0];       // doc owns the left thigh
    }
    pose.playKeyframes(keyed);
    at(motion, pose, 0.25);
    const dbg = pose.debug();
    assert.ok(!dbg.autoLegs.includes('leftUpperLeg'),
      'auto-legs overwrote the doc-keyed left thigh');
    assert.ok(dbg.autoLegs.includes('leftLowerLeg'),
      'unkeyed left shin must still derive');
    assert.ok(dbg.autoLegs.includes('rightUpperLeg'));
    // the doc key is what plays there (X +20 deg, scaled by the blend, w=1)
    const nb = pose._kfBone('leftUpperLeg');
    const relDeg = nb.rest.angleTo(nb.node.quaternion) * 180 / Math.PI;
    assert.ok(Math.abs(relDeg - 20) < 2.5, `keyed thigh wrote ${relDeg} deg`);
    // and the RIGHT planted foot is still pinned while the left thigh is keyed
    const m = legMetrics(motion);
    assert.ok(Math.abs(m.right.foot.y - rest.right.foot.y) <= 0.01,
      'right foot drifted while left thigh was keyed');
    // airborne: the unkeyed FOOT derives too (tuck points it)
    at(motion, pose, 0.6);
    assert.ok(pose.debug().autoLegs.includes('leftFoot'),
      'unkeyed left foot must derive in the tuck');
  });

  test('docs WITHOUT hips_y never touch the legs (unchanged behaviour)', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const armOnly = {
      name: 'arms_only', duration_s: 1.0, loop: false, ease: 'smooth',
      keyframes: [
        { t: 0, bones: {} },
        { t: 0.5, bones: { leftUpperArm: [0, 0, -90] } },
        { t: 1.0, bones: {} },
      ],
    };
    pose.playKeyframes(armOnly);
    sim(motion, pose, 0.9);
    const dbg = pose.debug();
    assert.deepEqual(dbg.autoLegs, []);
    for (const b of ['leftUpperLeg', 'leftLowerLeg', 'leftFoot',
                     'rightUpperLeg', 'rightLowerLeg', 'rightFoot']) {
      const nb = pose._kfBone(b);
      assert.equal(nb.rest.angleTo(nb.node.quaternion), 0, `${b} moved sans hips_y`);
    }
  });

  test('legacy victory_leap-style doc: no straight-legged airborne frame', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const legacy = {
      name: 'legacy_leap', duration_s: 3.0, loop: false, ease: 'smooth',
      keyframes: [
        { t: 0.0, bones: {}, expression: {}, hips_y: 0 },
        { t: 0.4, bones: {}, expression: {}, hips_y: -0.03 },
        { t: 0.9, bones: {}, expression: {}, hips_y: 0.12 },
        { t: 1.3, bones: {}, expression: {}, hips_y: -0.02 },
        { t: 1.7, bones: {}, expression: {}, hips_y: 0.02 },
        { t: 2.0, bones: {}, expression: {}, hips_y: 0.06 },
        { t: 2.3, bones: {}, expression: {}, hips_y: 0.1 },
        { t: 2.6, bones: {}, expression: {}, hips_y: -0.02 },
        { t: 3.0, bones: {}, expression: {}, hips_y: 0 },
      ],
    };
    pose.playKeyframes(legacy);
    const rest = legMetrics(motion);
    // sample the whole run: wherever the doc has hips_y well above 0, the
    // captured knee flex must read as a real bend, not a straight leg
    let airborneChecks = 0;
    for (let t = 0.05; t < 2.95; t += 0.05) {
      at(motion, pose, t);
      const hipsY = pose.kf.anim.keyframes.reduce((acc, k, i, ks) => {
        if (k.t <= t) acc = [k, ks[i + 1] || k];
        return acc;
      }, [null, null]);
      const [ka, kb] = hipsY;
      const u = kb && kb.t > ka.t ? (t - ka.t) / (kb.t - ka.t) : 0;
      const ez = u * u * (3 - 2 * u);
      const hy = (ka.hips_y || 0) + ((kb.hips_y || 0) - (ka.hips_y || 0)) * ez;
      if (hy <= 0.05) continue;
      airborneChecks += 1;
      const m = legMetrics(motion);
      assert.ok(m.left.kneeFlexDeg > rest.left.kneeFlexDeg + 20 &&
                m.right.kneeFlexDeg > rest.right.kneeFlexDeg + 20,
        `straight-legged airborne frame at t=${t.toFixed(2)} ` +
        `(L ${m.left.kneeFlexDeg.toFixed(1)} R ${m.right.kneeFlexDeg.toFixed(1)})`);
    }
    assert.ok(airborneChecks > 8, `only ${airborneChecks} airborne samples`);
  });

  // ── landing cushion (stateless doc-time sink under the touchdown) ──────
  const CROUCH_ONLY = {
    name: 'crouch_only', duration_s: 1.2, loop: false, ease: 'smooth',
    keyframes: [
      { t: 0.0, bones: {}, expression: {}, hips_y: 0 },
      { t: 0.3, bones: {}, expression: {}, hips_y: -0.06 },
      { t: 0.6, bones: {}, expression: {}, hips_y: -0.06 },
      { t: 1.2, bones: {}, expression: {}, hips_y: 0 },
    ],
  };

  test('cushion: doc without an airborne rise behaves byte-identically (c == 0)', need, () => {
    assert.deepEqual(landingTouchdowns(CROUCH_ONLY), []);
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    pose.playKeyframes(CROUCH_ONLY);
    at(motion, pose, 0.4);                 // inside the -0.06 dip
    assert.ok(Math.abs(motion._node('hips').position.y
      - motion._restPos.y + 0.06) < 1e-3, 'dip changed without an airborne rise');
    assert.equal(pose.debug().autoLegSolve.left.mode, 'planted');
    assert.equal(pose.kf.autoLegs.cushion(0.4), 0);
  });

  test('cushion: touchdown+0.05 — knees bent (>=25 deg by the dip key), feet planted', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const rest = legMetrics(motion);
    pose.playKeyframes(HOP);
    const td = landingTouchdowns(HOP);
    assert.equal(td.length, 1, `expected one landing, got ${JSON.stringify(td)}`);
    at(motion, pose, td[0] + 0.05);
    const m = legMetrics(motion);
    const dbg = pose.debug();
    // the doc is still at ~0 here (mid-descent): only the cushion is bent.
    // The absorb budget (kneeRatePerFrame=8 deg/frame) caps how deep the
    // body can be 3 frames after touchdown; by t_td+0.1 the sink is fully
    // underway and BOTH knees clear 25 deg.
    assert.equal(dbg.autoLegSolve.left.mode, 'planted');
    const sink = motion._restPos.y - motion._node('hips').position.y;
    assert.ok(sink > 0.02, `body not sinking at touchdown (sink ${sink.toFixed(4)} m)`);
    for (const side of ['left', 'right']) {
      const dy = Math.abs(m[side].foot.y - rest[side].foot.y);
      assert.ok(dy <= 0.01, `${side} foot ${dy.toFixed(4)} m off ground at touchdown+0.05`);
      assert.ok(m[side].kneeFlexDeg > rest[side].kneeFlexDeg + 3,
        `${side} knee not bending at touchdown+0.05 (${m[side].kneeFlexDeg.toFixed(1)})`);
    }
    at(motion, pose, td[0] + 0.1);
    const m2 = legMetrics(motion);
    assert.equal(pose.debug().autoLegSolve.left.mode, 'planted');
    for (const side of ['left', 'right']) {
      const dy = Math.abs(m2[side].foot.y - rest[side].foot.y);
      assert.ok(dy <= 0.01, `${side} foot ${dy.toFixed(4)} m off ground at touchdown+0.1`);
      assert.ok(m2[side].kneeFlexDeg >= 25,
        `${side} knee only ${m2[side].kneeFlexDeg.toFixed(1)} deg at touchdown+0.1`);
    }
  });

  test('cushion: landing dip key — knees >= 45 deg, planted', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    const rest = legMetrics(motion);
    pose.playKeyframes(HOP);
    at(motion, pose, 1.1);
    const m = legMetrics(motion);
    assert.equal(pose.debug().autoLegSolve.left.mode, 'planted');
    for (const side of ['left', 'right']) {
      const dy = Math.abs(m[side].foot.y - rest[side].foot.y);
      assert.ok(dy <= 0.01, `${side} foot ${dy.toFixed(4)} m off ground at dip key`);
      assert.ok(m[side].kneeFlexDeg >= 45,
        `${side} landing knee ${m[side].kneeFlexDeg.toFixed(1)} deg (gate 45)`);
    }
  });

  test('cushion: seeked final keyframe lands on EXACT rest', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    pose.playKeyframes(HOP);
    for (const t of [0.25, 0.6, 1.1, 0.85, 1.1]) at(motion, pose, t);   // jump around
    at(motion, pose, 1.5);                    // the final key (hips_y 0, c 0)
    for (const b of ['leftUpperLeg', 'leftLowerLeg', 'leftFoot',
                     'rightUpperLeg', 'rightLowerLeg', 'rightFoot']) {
      const nb = pose._kfBone(b);
      assert.equal(nb.rest.angleTo(nb.node.quaternion), 0, `${b} not exact rest at final key`);
    }
    assert.equal(motion._node('hips').position.y, motion._restPos.y);
  });

  test('cushion: deterministic across seeks — the same doc time twice is identical', need, () => {
    const { motion, pose } = makeRig();
    sim(motion, pose, 0.5);
    pose.playKeyframes(HOP);
    const snap = () => ({
      hipsY: motion._node('hips').position.y,
      feet: ['left', 'right'].map((s) => wp(motion, `${s}Foot`).toArray()),
      flex: pose.debug().autoLegSolve,
    });
    at(motion, pose, 0.93); const a = snap();
    at(motion, pose, 0.3); at(motion, pose, 1.35); at(motion, pose, 0.6);
    at(motion, pose, 0.93); const b = snap();
    assert.equal(b.hipsY, a.hipsY);
    a.feet.forEach((fa, i) => fa.forEach((v, j) => {
      assert.ok(Math.abs(b.feet[i][j] - v) < 1e-4,
        `foot moved between visits: ${fa} vs ${b.feet[i]}`);
    }));
    for (const side of ['left', 'right']) {
      assert.equal(b.flex[side].mode, a.flex[side].mode);
      assert.ok(Math.abs(b.flex[side].kneeFlexDeg - a.flex[side].kneeFlexDeg) < 0.25,
        `${side} knee drifted between visits: ${a.flex[side].kneeFlexDeg} vs ${b.flex[side].kneeFlexDeg}`);
    }
    // the curve itself is bit-exact and order-independent
    const c1 = landingCushion(HOP), c2 = landingCushion(HOP);
    for (const t of [0.8, 0.91, 1.0, 1.3]) assert.equal(c1(t), c2(t));
  });

  test('continuity at 60 fps: max knee change < 8 deg/frame over the whole doc', need, () => {
    // mild natural rest twist (~1:1 drive->world ratio like real rigs); the
    // violently twisted default rig composes the same hinge motion into a
    // faster WORLD knee angle — a rig-frame property, not a curve property.
    for (const doc of [HOP, {
      name: 'legacy_leap', duration_s: 3.0, loop: false, ease: 'smooth',
      keyframes: [
        { t: 0.0, bones: {}, expression: {}, hips_y: 0 },
        { t: 0.4, bones: {}, expression: {}, hips_y: -0.03 },
        { t: 0.9, bones: {}, expression: {}, hips_y: 0.12 },
        { t: 1.3, bones: {}, expression: {}, hips_y: -0.02 },
        { t: 1.7, bones: {}, expression: {}, hips_y: 0.02 },
        { t: 2.0, bones: {}, expression: {}, hips_y: 0.06 },
        { t: 2.3, bones: {}, expression: {}, hips_y: 0.1 },
        { t: 2.6, bones: {}, expression: {}, hips_y: -0.02 },
        { t: 3.0, bones: {}, expression: {}, hips_y: 0 },
      ],
    }]) {
      const { motion, pose } = makeRig(0.1);
      sim(motion, pose, 0.5);
      pose.playKeyframes(doc);
      let prev = null, worst = 0, worstAt = 0, worstAbs = 0, worstAbsAt = 0;
      const dur = doc.duration_s;
      let inLand = false, landFrame = 0, surgeFrame = 9, prevEff = null;
      for (let t = 1 / 60; t <= dur + 1e-9; t += 1 / 60) {
        at(motion, pose, Math.min(t, dur));
        const eff = pose.kf.autoLegs.eff(Math.min(t, dur));
        const landing = eff < -0.015;           // inside a landing absorb
        // Takeoff surge: the doc's own hips accelerate up through the ground
        // line at push-off peak (>20 mm/frame). The leg legitimately STRAIGHTS
        // fast there — that is the authored jump, not a solver pop — so the
        // first frames after it are budget-exempt like the landing absorb.
        const surge = prevEff !== null && eff - prevEff > 0.02;
        surgeFrame = surge ? 0 : surgeFrame + 1;
        prevEff = eff;
        const m = legMetrics(motion);
        if (prev) {
          for (const side of ['left', 'right']) {
            const d = Math.abs(m[side].kneeFlexDeg - prev[side].kneeFlexDeg);
            // the reference landing ramps ~9-10 deg/frame for its first
            // ~0.07 s (that IS the absorb); budget applies after that.
            const budgeted = !((landing && landFrame <= 4) || surgeFrame <= 1);
            if (budgeted && d > worst) { worst = d; worstAt = t; }
            if (d > worstAbs) { worstAbs = d; worstAbsAt = t; }
          }
        }
        landFrame = landing ? (inLand ? landFrame + 1 : 1) : 0;
        inLand = landing;
        prev = m;
      }
      assert.ok(worst < 8,
        `${doc.name}: knee jumped ${worst.toFixed(2)} deg/frame at t=${worstAt.toFixed(3)}`);
      assert.ok(worstAbs < 14,
        `${doc.name}: knee jumped ${worstAbs.toFixed(2)} deg/frame (absurd) at t=${worstAbsAt.toFixed(3)}`);
    }
  });
}
