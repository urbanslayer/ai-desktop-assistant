// node --test client/js/motion.js  (via: node --test client/js/)
//
// Regression harness for the "disembodied spinning head" bug: update() used
// to do `bone.rotation.x += ...` every frame without ever restoring the rest
// pose, so sway/gaze/tilt accumulated forever and the head spun in place.
// These tests run AvatarMotion against REAL three.js math (client/package.json
// devDependency, same version as the page's CDN import) and a fake VRM built
// from real THREE.Object3D bones, for 10 simulated minutes at 60 fps through
// idle + listening + thinking + speaking, and assert that every bone's
// rotation relative to rest stays inside the believable envelope *every
// frame*, with no drift late in the run.
//
// Skips cleanly when three isn't installed (`npm install` in client/).
import { test } from 'node:test';
import assert from 'node:assert/strict';

let THREE = null;
try {
  THREE = await import('three');
} catch { /* three not installed */ }

const need = { skip: THREE ? false : 'run `npm install` in client/ (three is a devDependency)' };

if (THREE) {
  const { AvatarMotion } = await import('./motion.js');
  const { Director } = await import('./director.js');
  const { GESTURES } = await import('./gestures.js');

  // deterministic Math.random for the whole motion layer (blinks, saccades,
  // glances, look-away picks)
  function seedRandom(seed) {
    const orig = Math.random;
    let s = seed >>> 0;
    Math.random = () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
    return () => { Math.random = orig; };
  }
  const seq = (...vals) => { let i = 0; return () => vals[i++ % vals.length]; };

  // ── fake VRM on a real Object3D skeleton ──
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
      // Arms-down rest. Real VRM rigs bake a per-side bone-axis frame into
      // each normalized bone, so index.html's ±1.3 is side-dependent there;
      // bare Object3D bones have no such frame, so the SIGN here is simply
      // whichever one makes the chain hang down-and-outward (verified by the
      // IK tests solving against this rig).
      ua.rotation.z = (side === 'right' ? -1.3 : 1.3);
      const la = mk(`${side}LowerArm`, ua, 0, -0.28, 0);
      mk(`${side}Hand`, la, 0, -0.25, 0);
    }
    // eyes (the rig profile measures head size from eye spacing when present)
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

  // offset of a bone from its attach()-time rest, as XYZ euler
  function offsetEuler(rest, q) {
    const off = rest.clone().invert().multiply(q);
    return new THREE.Euler().setFromQuaternion(off, 'XYZ');
  }
  const finite = (o) => Number.isFinite(o.x) && Number.isFinite(o.y) && Number.isFinite(o.z);

  // envelope relative to rest — motion.js HEAD_LIMITS + a little float slack
  const HEAD = { x: [-0.200001, 0.250001], y: [-0.350001, 0.350001], z: [-0.120001, 0.120001] };
  const MAX_COMBINED_YAW = 0.45;   // gesture + gaze on neck AND head, worst case
  const MAX_SPINE = 0.45;          // spine-chain bones are gesture-driven only
  const MAX_ARM_ANGLE = 2.7;       // rad between rest and current (stretch is ~2.3)

  function inBounds(e, lim) {
    return e.x >= lim.x[0] && e.x <= lim.x[1] && e.y >= lim.y[0] && e.y <= lim.y[1]
      && e.z >= lim.z[0] && e.z <= lim.z[1];
  }

  test('10 simulated minutes: bone offsets stay bounded at every frame, no drift', need, (t) => {
    const restore = seedRandom(20261104);
    t.after(restore);
    const vrm = fakeVRM();
    const camera = new THREE.PerspectiveCamera(30, 1.6, 0.1, 20);
    camera.position.set(0, 1.55, 2.0);
    camera.lookAt(0, 1.4, 0);
    const scene = new THREE.Scene();
    scene.add(vrm.scene);

    const motion = new AvatarMotion(THREE, { scene, camera });
    motion.attach(vrm);
    // snapshot the rest straight off the nodes (no motion.js internals), so
    // the old accumulating build fails on the drift bounds themselves
    const rest = new Map();
    for (const [b, n] of motion._boneCache) rest.set(b, n.quaternion.clone());
    assert.ok(rest.has('head') && rest.has('neck'), 'bones must exist');

    const director = new Director({ random: seq(0.5, 0.13, 0.87, 0.42, 0.61, 0.28, 0.77, 0.95) });
    const apply = (a) => {
      if (a.type === 'gesture') motion.playGesture(a.name);
      else if (a.type === 'gesture_stop') motion.stopGesture(a.name);
      else if (a.type === 'expression') {
        motion.applyEmotion(a.name, a.value);
        motion.tilt(a.tilt);
        if (a.away) motion.lookAway(a.hold || 2);
      }
    };
    const setStatus = (now, state) => {
      for (const a of director.status(now, state)) apply(a);
      motion.setGazeMode(['listening', 'hearing', 'speaking'].includes(state) ? 'camera' : 'free');
    };

    for (const a of director.start(0)) apply(a);  // welcome wave

    const dt = 1 / 60;
    const FRAMES = 600 * 60;
    // phases: idle 0-150, listening 150-300, thinking 300-450, speaking 450-600
    const phases = [[0, 'idle'], [150, 'listening'], [300, 'thinking'], [450, 'speaking']];
    const emotions = ['happy', 'teasing', 'sad', 'annoyed', 'embarrassed', 'surprised', 'neutral'];
    let emo = 0, phaseI = 0;
    setStatus(0, 'idle');

    const maxOffsetEarly = { head: 0, neck: 0 };
    let maxOffsetLate = 0, maxCombined = 0, worst = null;
    const checkBone = (name, lim, frames, angleMax) => {
      const node = motion._node(name);
      if (!node) return 0;
      const e = offsetEuler(rest.get(name), node.quaternion);
      if (!finite(e)) throw new Error(`${name} NaN at t=${(frames * dt).toFixed(2)}s`);
      if (!inBounds(e, lim)) throw new Error(
        `${name} out of bounds at t=${(frames * dt).toFixed(2)}s: ` +
        `x=${e.x.toFixed(3)} y=${e.y.toFixed(3)} z=${e.z.toFixed(3)}`);
      return Math.max(Math.abs(e.x), Math.abs(e.y), Math.abs(e.z));
    };

    for (let f = 1; f <= FRAMES; f++) {
      const now = f * dt;
      if (phaseI + 1 < phases.length && now >= phases[phaseI + 1][0]) {
        phaseI++;
        setStatus(now, phases[phaseI][1]);
      }
      if (f % Math.round(45 * 60) === 0) {  // a spoken line every 45 s
        emo++;
        for (const a of director.say(now, { emotion: emotions[emo % emotions.length], seconds: 3 })) apply(a);
      }
      for (const a of director.tick(now)) apply(a);
      motion.update(dt);

      const hn = checkBone('head', HEAD, f);
      const nn = checkBone('neck', HEAD, f);
      void hn; void nn;
      checkBone('spine', { x: [-MAX_SPINE, MAX_SPINE], y: [-MAX_SPINE, MAX_SPINE], z: [-MAX_SPINE, MAX_SPINE] }, f);
      for (const b of ['chest', 'upperChest', 'hips']) {
        const node = motion._node(b);
        if (!node) continue;
        const e = offsetEuler(rest.get(b), node.quaternion);
        if (!finite(e) || Math.max(Math.abs(e.x), Math.abs(e.y), Math.abs(e.z)) > MAX_SPINE) {
          throw new Error(`${b} wild at t=${now.toFixed(2)}s`);
        }
      }
      for (const b of ['leftUpperArm', 'rightUpperArm', 'leftLowerArm', 'rightLowerArm']) {
        const node = motion._node(b);
        if (!node) continue;
        if (!node.quaternion.isFinite?.() && ![node.quaternion.x, node.quaternion.y, node.quaternion.z, node.quaternion.w].every(Number.isFinite)) {
          throw new Error(`${b} NaN quaternion at t=${now.toFixed(2)}s`);
        }
        const ang = 2 * Math.acos(Math.min(1, Math.abs(rest.get(b).dot(node.quaternion))));
        if (ang > MAX_ARM_ANGLE) throw new Error(`${b} ${ang.toFixed(2)} rad from rest at t=${now.toFixed(2)}s`);
      }

      // head+neck combined yaw (the "head attached and believable" check)
      const hy = offsetEuler(rest.get('head'), motion._node('head').quaternion).y;
      const ny = offsetEuler(rest.get('neck'), motion._node('neck').quaternion).y;
      maxCombined = Math.max(maxCombined, Math.abs(hy) + Math.abs(ny));
      if (maxCombined > MAX_COMBINED_YAW && !worst) {
        worst = `t=${now.toFixed(2)}s head ${hy.toFixed(3)} + neck ${ny.toFixed(3)}`;
      }

      const headAng = 2 * Math.acos(Math.min(1, Math.abs(rest.get('head').dot(motion._node('head').quaternion))));
      if (now <= 120) maxOffsetEarly.head = Math.max(maxOffsetEarly.head, headAng);
      if (now >= 480) maxOffsetLate = Math.max(maxOffsetLate, headAng);
    }

    // 1. every frame was inside the envelope (checkBone throws otherwise)
    // 2. combined head+neck yaw stayed a believable turn
    assert.equal(worst, null, `combined head+neck yaw exceeded ${MAX_COMBINED_YAW}: ${worst}`);
    // 3. NO DRIFT: the head is no more rotated at t=480..600 s than in the
    //    first two minutes (the old code grew until it spun full circles)
    assert.ok(maxOffsetLate <= maxOffsetEarly.head + 0.02,
      `head drifts: early max ${maxOffsetEarly.head.toFixed(3)} rad, late max ${maxOffsetLate.toFixed(3)} rad`);
    const finalAng = 2 * Math.acos(Math.min(1, Math.abs(rest.get('head').dot(motion._node('head').quaternion))));
    assert.ok(finalAng < 0.35, `head not settled back into envelope at t=600s: ${finalAng.toFixed(3)} rad`);
    // 4. sanity: she did move (the layer is alive, not frozen)
    assert.ok(maxOffsetEarly.head > 0.001, 'head never moved at all');
  });

  test('repeated update() at rest never accumulates (old += bug)', need, () => {
    const restore = seedRandom(7);
    try {
      const vrm = fakeVRM();
      const camera = new THREE.PerspectiveCamera(30, 1.6, 0.1, 20);
      camera.position.set(0, 1.55, 2.0);
      const scene = new THREE.Scene();
      scene.add(vrm.scene);
      const motion = new AvatarMotion(THREE, { scene, camera });
      motion.attach(vrm);
      // snapshot rest from the nodes themselves (no motion.js internals) so
      // this test also provably catches the old per-frame `+=` accumulation
      const rest = new Map();
      for (const [b, n] of motion._boneCache) rest.set(b, n.quaternion.clone());
      const neck = motion._node('neck');
      for (let i = 0; i < 60 * 300; i++) motion.update(1 / 60);  // 5 minutes, no gestures
      const e = offsetEuler(rest.get('neck'), neck.quaternion);
      assert.ok(finite(e) && Math.abs(e.y) < 0.4 && Math.abs(e.x) < 0.3 && Math.abs(e.z) < 0.15,
        `neck accumulated to ${JSON.stringify(e)} after 5 idle minutes`);
      const headE = offsetEuler(rest.get('head'), motion._node('head').quaternion);
      assert.ok(finite(headE) && Math.max(Math.abs(headE.x), Math.abs(headE.y), Math.abs(headE.z)) < 0.4,
        `head accumulated to ${JSON.stringify(headE)} after 5 idle minutes`);
      // eyes-then-head: look-away starts at the eyes, the head follows slowly
      const before = motion.__debug().followYaw;
      motion.lookAway(2.5);
      for (let i = 0; i < 6; i++) motion.update(1 / 60);  // 0.1 s
      assert.notEqual(motion.__debug().followYaw, before, 'head follow never engaged');
      assert.ok(Math.abs(motion.__debug().followYaw) < 0.31, 'follow stayed in budget');
    } finally { restore(); }
  });

  test('gestures return to the arms-down rest, no T-pose, no leftovers', need, () => {
    const restore = seedRandom(11);
    try {
      const vrm = fakeVRM();
      const camera = new THREE.PerspectiveCamera(30, 1.6, 0.1, 20);
      camera.position.set(0, 1.55, 2.0);
      const scene = new THREE.Scene();
      scene.add(vrm.scene);
      const motion = new AvatarMotion(THREE, { scene, camera });
      motion.attach(vrm);
      for (const name of Object.keys(GESTURES)) {
        motion.playGesture(name);
        const dur = GESTURES[name].duration;
        for (let s = 0; s < dur + 1.2; s += 1 / 60) motion.update(1 / 60);
        if (motion.gesture) motion.stopGesture(name);
        for (let s = 0; s < 1.0; s += 1 / 60) motion.update(1 / 60);
        const ua = motion._node('rightUpperArm');
        const ang = 2 * Math.acos(Math.min(1, Math.abs(motion._rest.get('rightUpperArm').dot(ua.quaternion))));
        // small ambient offsets are fine; an arm still 60+ deg out is a leftover
        assert.ok(ang < 0.6, `${name}: rightUpperArm ${ang.toFixed(2)} rad from rest after it ended`);
      }
      const ra = offsetEuler(motion._rest.get('rightUpperArm'), motion._node('rightUpperArm').quaternion);
      assert.ok(Math.hypot(ra.x, ra.y, ra.z) < 0.2, 'arm should hang down at rest');
      assert.equal(motion.gesture, null);
      assert.equal(motion._ghost, null);
    } finally { restore(); }
  });
}
