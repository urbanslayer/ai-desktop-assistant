// node --test client/js/pose.js — limb-grab IK, joint limits, torso-capsule
// clamp, spring-back and the sit pose, on a fake rig with REAL three.js math
// (fakeVRM copied from motion.test.js) through the real AvatarMotion +
// PoseLayer. Skips cleanly when three isn't installed (`npm install` in client/).
import { test } from 'node:test';
import assert from 'node:assert/strict';

let THREE = null;
try {
  THREE = await import('three');
} catch { /* three not installed */ }

const need = { skip: THREE ? false : 'run `npm install` in client/ (three is a devDependency)' };

if (THREE) {
  const { AvatarMotion } = await import('./motion.js');
  const {
    PoseLayer, GRAB_LIMITS, HEAD_GRAB_LIMITS, clampOutsideCapsule, clampGrabTarget,
  } = await import('./pose.js');

  // ── fake VRM on a real Object3D skeleton (same as motion.test.js) ──
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
      mk(`${side}Hand`, la, 0, -0.25, 0);
    }
    for (const [side, s] of [['left', 1], ['right', -1]]) {
      const ul = mk(`${side}UpperLeg`, hips, 0.08 * s, -0.05, 0);
      const ll = mk(`${side}LowerLeg`, ul, 0, -0.42, 0);
      mk(`${side}Foot`, ll, 0, -0.40, 0.05);
    }
    mk('eyeLeft', bones.neck, 0.032, 0.05, 0.05);
    mk('eyeRight', bones.neck, -0.032, 0.05, 0.05);
    return {
      meta: { metaVersion: '1' },
      scene,
      bones,
      humanoid: { getNormalizedBoneNode: (n) => bones[n] || null },
      lookAt: { autoUpdate: true, target: null },
      expressionManager: { setValue() {} },
    };
  }

  function makePoseRig() {
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

  const live = (motion, bone) => {
    motion.vrm.scene.updateMatrixWorld(true);
    return motion._node(bone).getWorldPosition(new THREE.Vector3());
  };

  function sim(motion, pose, seconds, step = null) {
    const dt = 1 / 60;
    for (let i = 0; i < Math.round(seconds * 60); i++) {
      motion.update(dt);
      pose.apply(dt);
      if (step) step(i);
    }
  }

  test('capsule clamp pushes points out of the torso axis', need, () => {
    const a = [0, 1, 0], b = [0, 1.4, 0];
    const p = clampOutsideCapsule([0.05, 1.2, 0.05], a, b, 0.2);
    assert.ok(Math.hypot(p[0], p[2]) >= 0.2 - 1e-9);
    const outside = [0.9, 1.1, 0];
    assert.deepEqual(clampOutsideCapsule(outside, a, b, 0.2), outside);
    const onAxis = clampOutsideCapsule([0, 1.2, 0], a, b, 0.2);
    assert.ok(Math.hypot(onAxis[0], onAxis[2]) >= 0.2 - 1e-6);
  });

  test('grab hand: wrist lands on the target; release restores rest exactly', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.5);
    const restW = live(motion, 'leftHand');
    const target = [0.55, 1.75, 0.15];        // up-and-out, reachable
    pose.setTarget('hand_l', target);
    sim(motion, pose, 2.0);
    const w = live(motion, 'leftHand');
    assert.ok([w.x, w.y, w.z].every(Number.isFinite));
    assert.ok(w.distanceTo(new THREE.Vector3(...target)) < 0.045,
      `wrist ${w.toArray().map((v) => v.toFixed(3))} vs target ${target}`);
    pose.release('hand_l');
    sim(motion, pose, 0.6);
    // rest pose restored EXACTLY (quaternion-level: immune to life-layer sway)
    for (const bn of ['leftUpperArm', 'leftLowerArm']) {
      const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
      assert.ok(ang < 1e-4, `${bn} drift ${ang}`);
    }
    assert.equal(pose.grabbedIds().length, 0);
  });

  test('grab: extremes stay in reach, outside the capsule, inside limits, no NaN', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.3);
    const extremes = [[3, 3, 3], [-3, -1, -3], [0, 1.2, 0], [0.05, 1.2, 0.15], [10, 1.3, 0]];
    const bad = [];
    const arm = motion.rig.arms.left;
    const lTot = arm.l1 + arm.l2;
    for (const t of extremes) {
      pose.setTarget('hand_l', t);
      sim(motion, pose, 1.5, () => {
        const w = live(motion, 'leftHand');
        if (![w.x, w.y, w.z].every(Number.isFinite)) bad.push(['nan', t]);
        // live shoulder (life-layer sway moves the whole arm base; comparing
        // against the REST shoulder would flag reach for body motion alone)
        const S = motion._node('leftUpperArm').getWorldPosition(new THREE.Vector3());
        if (w.distanceTo(S) > lTot + 1e-3) bad.push(['over-reach', w.distanceTo(S)]);
        const cap = pose.torso;
        if (w.y > cap.a[1] + 0.02 && w.y < cap.b[1] - 0.02) {
          const axisD = Math.hypot(w.x - cap.a[0], w.z - cap.a[2]);
          if (axisD < cap.r - 0.05) bad.push(['inside-capsule', +axisD.toFixed(3), t]);
        }
        for (const bn of ['leftUpperArm', 'leftLowerArm']) {
          const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
          if (ang > GRAB_LIMITS[bn] + 1e-3) bad.push(['limit', bn, +ang.toFixed(3)]);
        }
      });
      pose.release('hand_l');
      sim(motion, pose, 0.5);
    }
    assert.deepEqual(bad.slice(0, 6), []);
  });

  // ── reviewer regressions (T1 review: the first capture spec dragged 0 px,
  // so a grab that did NOTHING passed; these pin the visible-follow itself) ──
  test('grab regression: the hand visibly FOLLOWS a far raw target', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.5);
    const restW = live(motion, 'leftHand');
    const target = [0.6, 1.6, 0.1];         // far up-out from the resting wrist
    pose.setTarget('hand_l', target);
    sim(motion, pose, 1.2);                 // deep inside the hold phase
    const w = live(motion, 'leftHand');
    const targetDist = new THREE.Vector3(...target).distanceTo(restW);
    const disp = w.distanceTo(restW);
    assert.ok(targetDist > 0.25, `spec sanity: target only ${targetDist.toFixed(3)} m from rest`);
    // reviewer gate: displacement >= 60 % of the raw target's distance from rest
    assert.ok(disp >= 0.6 * targetDist,
      `hand moved ${disp.toFixed(3)} m but the target is ${targetDist.toFixed(3)} m away — grab is a no-op`);
    assert.ok(w.distanceTo(new THREE.Vector3(...target)) < 0.05,
      `wrist ${w.toArray().map((v) => +v.toFixed(3))} never reached the reachable target`);
  });

  test('release regression: arm returns to rest after a far grab (shake release)', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.5);
    const restW = live(motion, 'leftHand');
    pose.setTarget('hand_l', [0.6, 1.6, 0.1]);
    sim(motion, pose, 1.2);
    pose.release('hand_l', { shake: true });
    sim(motion, pose, 0.8);
    const w = live(motion, 'leftHand');
    // world-position gate (reviewer's "arm returns to rest"); the quaternion
    // check below is the drift-proof companion, immune to life-layer breath
    assert.ok(w.distanceTo(restW) < 0.03,
      `hand ${w.toArray().map((v) => +v.toFixed(3))} not back near rest ${restW.toArray().map((v) => +v.toFixed(3))}`);
    for (const bn of ['leftUpperArm', 'leftLowerArm']) {
      const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
      assert.ok(ang < 1e-4, `${bn} drift ${ang}`);
    }
    assert.equal(pose.grabbedIds().length, 0);
  });

  test('pat headTilt: peak roll 8-12 deg HELD through the pat, eased out ~0.8 s, head only', need, () => {
    const { motion, pose } = makePoseRig();
    motion.setGazeOverride({ yaw: 0, pitch: 0 });
    sim(motion, pose, 0.3);
    pose.react('headTilt', { side: 1 });
    const samples = [];
    sim(motion, pose, 1.8, () => {
      const d = pose.debug();
      if (typeof d.patRollDeg === 'number') samples.push(d.patRollDeg);
    });
    const peak = Math.max(...samples.map((r) => Math.abs(r)));
    assert.ok(peak >= 8 && peak <= 12, `pat peak roll ${peak.toFixed(2)} deg outside 8..12`);
    // held (not a decaying blip): across the middle 0.30-0.75 s the tilt stays
    // within 10 % of peak — the old exp-decay envelope failed this
    const mid = samples.slice(Math.round(0.30 * 60), Math.round(0.75 * 60)).map((r) => Math.abs(r));
    assert.ok(mid.length > 20, 'not enough hold samples');
    assert.ok(Math.min(...mid) >= 0.9 * peak,
      `tilt not held: dips to ${Math.min(...mid).toFixed(2)} deg vs peak ${peak.toFixed(2)}`);
    // fully eased out at the end of the window (roll hits 0 exactly at dur;
    // the last three frames before the reaction ends must be all but gone)
    const tail = samples.slice(-3).map((r) => Math.abs(r));
    assert.ok(Math.max(...tail) < 0.15, `ease-out incomplete: ${tail}`);
    // head bone only (garment skinning rule)
    for (const bn of ['neck', 'chest', 'spine', 'upperChest']) {
      const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
      assert.ok(ang < 0.08, `${bn} rotated ${ang} rad by the pat`);
    }
    assert.ok((pose.debug().patPeakRollDeg || 0) >= 8, 'debug().patPeakRollDeg never recorded');
  });

  test('head grab turns ONLY the head bone, inside the gaze envelope', need, () => {
    const { bones, motion, pose } = makePoseRig();
    // freeze gaze: the head delta must then be PURELY the pose overlay —
    // otherwise idle gaze wander makes the clamp/spring-back assertions race
    motion.setGazeOverride({ yaw: 0, pitch: 0 });
    sim(motion, pose, 0.3);
    pose.setTarget('head', [-1.5, 1.8, 0.5]);    // far up-left: clamp test
    sim(motion, pose, 1.5);
    const dq = motion._rest.get('head').clone().invert().multiply(bones.head.quaternion);
    const e = new THREE.Euler().setFromQuaternion(dq);
    assert.ok(e.x >= HEAD_GRAB_LIMITS.x[0] - 1e-3 && e.x <= HEAD_GRAB_LIMITS.x[1] + 1e-3, `pitch ${e.x}`);
    assert.ok(e.y >= HEAD_GRAB_LIMITS.y[0] - 1e-3 && e.y <= HEAD_GRAB_LIMITS.y[1] + 1e-3, `yaw ${e.y}`);
    assert.ok(e.z >= HEAD_GRAB_LIMITS.z[0] - 1e-3 && e.z <= HEAD_GRAB_LIMITS.z[1] + 1e-3, `roll ${e.z}`);
    // no neck/chest/spine rotation to fake the reach (breath stays tiny)
    for (const bn of ['neck', 'chest', 'spine', 'upperChest']) {
      const ang = motion._rest.get(bn).angleTo(bones[bn].quaternion);
      assert.ok(ang < 0.08, `${bn} rotated ${ang} rad`);
    }
    pose.release('head');
    sim(motion, pose, 0.6);
    const restAng = motion._rest.get('head').angleTo(bones.head.quaternion);
    assert.ok(restAng < 0.05, `head did not spring back (${restAng})`);
  });

  test('foot grab moves the leg by its own bones and springs back', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.3);
    const restA = live(motion, 'leftFoot');
    const target = [-0.2, 0.7, 0.45];           // forward-left lift
    pose.setTarget('foot_l', target);
    sim(motion, pose, 2.0);
    const a = live(motion, 'leftFoot');
    assert.ok(a.distanceTo(new THREE.Vector3(...target)) < 0.07, `ankle ${a.toArray()}`);
    pose.release('foot_l');
    sim(motion, pose, 0.6);
    for (const bn of ['leftUpperLeg', 'leftLowerLeg']) {
      const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
      assert.ok(ang < 1e-4, `${bn} ankle drift ${ang}`);
    }
  });

  test('sit: hips pin to the edge height, thighs forward, shins hanging', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.3);
    const edgeY = motion._restPos.y - 0.38;     // roughly a 70%-height screen edge
    pose.sitWorld(edgeY);
    sim(motion, pose, 0.9);
    assert.ok(pose.sitBlendDone, 'sit blend should be done');
    const hips = live(motion, 'hips');
    assert.ok(Math.abs(hips.y - edgeY) < 0.01, `hip y ${hips.y} vs edge ${edgeY}`);
    const hipJ = live(motion, 'leftUpperLeg');
    const knee = live(motion, 'leftLowerLeg');
    const ankle = live(motion, 'leftFoot');
    assert.ok(knee.z - hipJ.z > 0.15, `thigh not forward (${(knee.z - hipJ.z).toFixed(3)})`);
    assert.ok(knee.y - ankle.y > 0.25, `shin not hanging (${(knee.y - ankle.y).toFixed(3)})`);
    assert.ok(Math.abs(ankle.z - knee.z) < 0.18);
    assert.ok(pose.active, 'pose.active must hold while sitting (page freezes walk)');
    pose.stand();
    sim(motion, pose, 0.7);
    assert.ok(pose.standBlendDone);
    assert.ok(!pose.active);
    const rest = live(motion, 'hips');
    assert.ok(Math.abs(rest.y - motion._restPos.y) < 1e-4, 'hips not back at rest');
    assert.equal(pose.sitState, null);
  });

  test('sit: MEASURED leg angles pass the seated gates at any edge depth', need, () => {
    // Reviewer gates (docs/TOUCH.md round T1): thighs within 30 deg of
    // horizontal pointing toward the viewer (+z), knee flex 70-110 deg,
    // shins within 25 deg of vertical — at the shallow AND the deep edge.
    // The old depth-adaptive flex decay failed these at the deep edge.
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.3);
    for (const drop of [0.38, 0.62]) {          // ~70% and ~85% window edges
      pose.sitWorld(motion._restPos.y - drop);
      sim(motion, pose, 1.0);
      assert.ok(pose.sitBlendDone, `blend never finished (drop ${drop})`);
      const dbg = pose.debug();
      for (const side of ['left', 'right']) {
        const a = dbg.legAngles[side];
        assert.ok(a, `no legAngles for ${side}`);
        assert.ok(Math.abs(a.thighBelowHoriz) <= 30,
          `drop ${drop} ${side}: thigh pitch ${a.thighBelowHoriz} deg from horizontal`);
        assert.ok(a.thighZ > 0.05, `drop ${drop} ${side}: thigh not toward viewer (z=${a.thighZ})`);
        assert.ok(a.kneeFlex >= 70 && a.kneeFlex <= 110,
          `drop ${drop} ${side}: knee flex ${a.kneeFlex} deg outside 70..110`);
        assert.ok(a.shinFromVertical <= 25,
          `drop ${drop} ${side}: shin ${a.shinFromVertical} deg from vertical`);
      }
      pose.stand();
      sim(motion, pose, 0.7);
      assert.ok(pose.standBlendDone);
    }
  });

  test('sit px branch: hips converge onto the window edge line (camera space)', need, () => {
    const { motion, pose } = makePoseRig();
    motion.setGazeOverride({ yaw: 0, pitch: 0 });
    sim(motion, pose, 0.3);
    const viewH = 680;
    const pxY = () => {
      motion.vrm.scene.updateMatrixWorld(true);
      const p = motion._node('hips').getWorldPosition(new THREE.Vector3()).project(motion.camera);
      return (1 - p.y) / 2 * viewH;
    };
    const start = pxY();
    const edge = start + 150;                      // a line 150 px BELOW the hips
    pose.sit(edge, viewH);
    sim(motion, pose, 1.5);
    const landed = pxY();
    assert.ok(Math.abs(landed - edge) < 6,
      `hips px ${landed.toFixed(1)} vs edge ${edge} (started ${start.toFixed(1)})`);
    // and re-pinning to a new line still converges (sit:476 -> sit:578 flow)
    pose.sit(edge - 120, viewH);
    sim(motion, pose, 1.5);
    assert.ok(Math.abs(pxY() - (edge - 120)) < 6, `re-pin failed: ${pxY().toFixed(1)}`);
  });

  test('reactions decay: flinch ends at 0, head tilt/giggle vanish after their window', need, () => {
    const { motion, pose } = makePoseRig();
    sim(motion, pose, 0.2);
    pose.react('flinch', { dir: -1 });
    pose.react('headTilt', { side: 1 });
    pose.react('giggle', {});
    let maxF = 0;
    sim(motion, pose, 0.4, () => { maxF = Math.max(maxF, Math.abs(pose.flinchX)); });
    assert.ok(maxF > 0.005, 'flinch never moved her');
    sim(motion, pose, 2.0);
    assert.equal(pose.flinchX, 0);
    assert.equal(pose.reacts.size, 0);
    assert.ok(!pose.active);
  });

  test('10-minute soak: limits, capsule, NaN, drift on a full grab/sit/reaction cycle', need, () => {
    const { motion, pose } = makePoseRig();
    const dt = 1 / 60;
    const bad = [];
    // chain bones that must be BACK on rest 0.6 s after each phase starts
    // (the release happened at the previous phase's last frame; spring-back
    // is 0.4 s, so by frame 40 of the next phase drift must be zero)
    const RESTORED_BY = {
      grabR: ['leftUpperArm', 'leftLowerArm'],
      foot: ['rightUpperArm', 'rightLowerArm'],
      head: ['leftUpperLeg', 'leftLowerLeg'],
      pat: ['leftUpperLeg', 'leftLowerLeg', 'rightUpperLeg', 'rightLowerLeg'],
    };
    let restWrist = null;
    for (let c = 0; c < 20; c++) {
      const phases = [
        ['grabL', 3], ['grabR', 3], ['foot', 3], ['head', 3], ['sit', 4],
        ['pat', 1], ['poke', 1], ['tickle', 1], ['idle', 11],
      ];
      let t = c * 30;
      for (const [phase, dur] of phases) {
        if (phase === 'grabL') pose.setTarget('hand_l', [0.55, 1.75, 0.15]);
        if (phase === 'grabR') { pose.release('hand_l'); pose.setTarget('hand_r', [-0.55, 1.75, 0.15]); }
        if (phase === 'foot') { pose.release('hand_r'); pose.setTarget('foot_l', [-0.2, 0.7, 0.45]); }
        if (phase === 'head') { pose.release('foot_l'); pose.setTarget('head', [0.8, 1.85, 0.6]); }
        if (phase === 'sit') { pose.release('head'); pose.sitWorld(0.5 + (c % 3) * 0.05); }
        if (phase === 'pat') { pose.stand(); pose.react('headTilt', { side: c % 2 ? 1 : -1 }); }
        if (phase === 'poke') pose.react('flinch', { dir: c % 2 ? 1 : -1 });
        if (phase === 'tickle') pose.react('giggle', {});
        for (let i = 0; i < dur * 60; i++) {
          t += dt;
          motion.update(dt);
          pose.apply(dt);
          if (i === 40) {   // 0.667 s into the phase: previous grab fully released
            for (const bn of RESTORED_BY[phase] || []) {
              const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
              if (!(ang < 2e-4)) bad.push([+t.toFixed(1), 'not-restored', bn, +ang.toFixed(5)]);
            }
            // the head GRAB must be gone too — but gaze legitimately owns the
            // head bone afterwards, so completion is checked at grab level
            if (phase === 'sit' && (pose.grabbedIds().length !== 0 || pose.debug().sitting !== true)) {
              bad.push([+t.toFixed(1), 'head-grab-stuck', pose.grabbedIds().join('+')]);
            }
            if (phase === 'pat') {   // stand happened at the phase switch
              const hipsY = motion._node('hips').position.y;
              if (!(Math.abs(hipsY - motion._restPos.y) < 1e-3)) {
                bad.push([+t.toFixed(1), 'stand-slow', +hipsY.toFixed(5)]);
              }
            }
          }
          if (i % 7 === 0) {
            for (const [bn, lim] of Object.entries(GRAB_LIMITS)) {
              const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
              if (!(ang <= lim + 1e-3)) bad.push([+t.toFixed(1), bn, +ang.toFixed(3)]);
            }
            const w = live(motion, 'leftHand');
            if (![w.x, w.y, w.z].every(Number.isFinite)) bad.push([+t.toFixed(1), 'nan']);
            const cap = pose.torso;
            if (w.y > cap.a[1] + 0.02 && w.y < cap.b[1] - 0.02 &&
                Math.hypot(w.x - cap.a[0], w.z - cap.a[2]) < cap.r - 0.05) {
              bad.push([+t.toFixed(1), 'capsule']);
            }
          }
        }
        if (phase === 'idle') {
          // 11 s after the last release (0.4 s spring-back): every pose-driven
          // bone sits EXACTLY on rest — quaternion-level, sway cannot fake it
          for (const bn of Object.keys(GRAB_LIMITS)) {
            const ang = motion._rest.get(bn).angleTo(motion._node(bn).quaternion);
            if (!(ang < 1e-4)) bad.push([+t.toFixed(1), 'drift', bn, +ang.toFixed(5)]);
          }
          const hipsY = motion._node('hips').position.y;
          if (!(Math.abs(hipsY - motion._restPos.y) < 1e-3)) {
            bad.push([+t.toFixed(1), 'hip-drift', +hipsY.toFixed(5)]);
          }
        }
      }
    }
    assert.ok(bad.length === 0, `${bad.length} violations: ${JSON.stringify(bad.slice(0, 8))}`);
  });

  // ── clampGrabTarget: cursor-ray reach clamp (docs/TOUCH.md live-issue notes) ──
  const distToRay = (p, c, u) => {
    const w = p.map((v, i) => v - c[i]);
    const x = [w[1] * u[2] - w[2] * u[1], w[2] * u[0] - w[0] * u[2], w[0] * u[1] - w[1] * u[0]];
    return Math.hypot(...x);
  };

  test('clampGrabTarget passes through points already within reach', () => {
    const T = [0.35, 1.0, 0.2];
    assert.deepEqual(clampGrabTarget([0, 1.5, 2], [0, 0, -1], [0.2, 1.25, 0], 0.5, T), T);
  });

  test('clampGrabTarget aims the cursor ray at the near side of the reach sphere', () => {
    const C = [0, 1.6, 2], J = [0, 1.25, 0], r = 0.5;
    const dir = J.map((v, i) => v - C[i]);
    const T = J.map((v, i) => v + 4 * dir[i]);   // a raw point far past the joint
    const p = clampGrabTarget(C, dir, J, r, T);
    assert.ok(Math.abs(Math.hypot(...p.map((v, i) => v - J[i])) - r) < 1e-6, 'on the sphere');
    const u = dir.map((v) => v / Math.hypot(...dir));
    assert.ok(distToRay(p, C, u) < 1e-6, 'lands under the cursor ray, not off to the side');
    assert.ok(p[2] > J[2], 'camera-side intersection (hand toward the viewer)');
  });

  test('clampGrabTarget far drag: the clamp point hugs the cursor, unlike the along-ray clamp', () => {
    // regression for the live complaint "moving her hand is quite tricky":
    // a measured headless grab dragged the cursor ~198 px but the hand moved
    // only 43 px, because IK clamps ALONG the shoulder->target ray and that
    // ray's screen projection drifts away from the cursor. Geometry here is
    // the measured one (camera ~[0,1.5,1.9], left shoulder, reach 0.4345,
    // raw cursor-plane point [0.72,0.42,0.07]).
    const C = [0, 1.5, 1.9], J = [0.2, 1.25, 0], r = 0.4345, T = [0.72, 0.42, 0.07];
    const dir = T.map((v, i) => v - C[i]);
    const u = dir.map((v, i) => v / Math.hypot(...dir));
    const p = clampGrabTarget(C, dir, J, r, T);
    const naive = J.map((v, i) => v + (T[i] - v) * (r / Math.hypot(...T.map((v, i2) => v - J[i2]))));
    assert.ok(p.every(Number.isFinite));
    assert.ok(Math.abs(Math.hypot(...p.map((v, i) => v - J[i])) - r) < 1e-6,
              'reachable: sits exactly on the IK sphere, so IK cannot drift further');
    assert.ok(distToRay(p, C, u) < distToRay(naive, C, u),
              `clamp must follow the cursor better than the old along-ray clamp ` +
              `(${distToRay(p, C, u).toFixed(3)} vs ${distToRay(naive, C, u).toFixed(3)})`);
  });

  test('clampGrabTarget survives degenerate inputs', () => {
    const a = clampGrabTarget([0, 1.5, 2], [0, 0, 0], [0, 1, 0], 0.5, [0, 3, 0]);
    assert.ok(Math.abs(Math.hypot(...a.map((v, i) => v - [0, 1, 0][i])) - 0.5) < 1e-6,
              'zero ray falls back to the along-ray clamp');
    const b = clampGrabTarget([0, 1, 0], [0, -1, -2], [0, 1, 0], 0, [0, -5, -3]);
    assert.ok(b.every(Number.isFinite), 'camera on the joint + zero reach stays finite');
    assert.ok(Math.abs(Math.hypot(...b.map((v, i) => v - [0, 1, 0][i])) - 0.05) < 1e-6,
              'zero reach floors to the 5 cm minimum and stays on the sphere');
  });
}
