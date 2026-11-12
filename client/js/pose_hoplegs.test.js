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

let THREE = null;
try {
  THREE = await import('three');
} catch { /* three not installed */ }

const need = { skip: THREE ? false : 'run `npm install` in client/ (three is a devDependency)' };

if (THREE) {
  const { AvatarMotion } = await import('./motion.js');
  const { PoseLayer } = await import('./pose.js');

  // ── fake VRM whose legs carry non-identity rest rotations ──────────────
  function twistedVRM() {
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
      ul.rotation.set(-0.35, 1.45 * s, 0.12 * s);    // non-identity, twisted
      const ll = mk(`${side}LowerLeg`, ul, 0, -0.42, 0);
      ll.rotation.set(0.2, -1.1 * s, 0.15 * s);      // knee hangs forward+in
      const ft = mk(`${side}Foot`, ll, 0, -0.40, 0);
      ft.rotation.set(0.8, 0.2 * s, -0.5 * s);       // foot frame twisted too
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

  function makeRig() {
    const vrm = twistedVRM();
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
    at(motion, pose, 0.55);                    // near top of the push-off
    const a = legMetrics(motion).left.foot.clone();
    at(motion, pose, 0.65);
    const b = legMetrics(motion).left.foot.clone();
    assert.ok(a.distanceTo(b) < 0.08, `foot jumped ${a.distanceTo(b).toFixed(3)} m`);
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
}
