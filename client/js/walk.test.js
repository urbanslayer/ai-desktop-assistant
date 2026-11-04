// node --test client/js/walk.js (via: node --test client/js/)
//
// Docs/WALK_MEMORY.md §W verification, pure layer:
//   - the roam schedule (fires inside the configured gap, never while busy,
//     mid-walk interruptions stop smoothly, edges turn her around, x is
//     clamped and root motion integrates at the configured speed);
//   - the Director interplay (natural gestures wait while she walks; forced
//     ones — W, asked-for actions, persona waves — stop the walk FIRST);
//   - the walk cycle over a 10-minute simulation: every bone offset stays
//     inside WALK_LIMITS for every phase, knee never hyperextends, and the
//     envelope scales with leg length (proportion-aware).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  WalkDirector, walkPose, walkPoseEnvelope, strideLen, cadence,
  thighAmp, kneeMax, WALK_LIMITS, WALK_BONES,
} from './walk.js';
import { Director } from './director.js';

const seq = (...vals) => { let i = 0; return () => vals[i++ % vals.length]; };

function madeWalker(opts = {}, random = seq(0.5)) {
  const w = new WalkDirector({ random, every: [10, 12], ...opts });
  w.configure({ height: 1.5, bounds: { min: -2.0, max: 2.0 } });
  return w;
}

// ── schedule ───────────────────────────────────────────────────────
test('roam starts inside the configured idle gap and walks to a target', () => {
  const w = madeWalker();
  w.armIdle(0);                                   // armed at now+10..12
  let acts = [];
  for (let t = 0; t <= 12.1; t += 0.1) acts.push(...w.tick(t, 0.1));
  const start = acts.find((a) => a.type === 'walk_start');
  assert.ok(start, 'no roam started within the gap');
  assert.ok(w.walking);
  assert.ok(Math.abs(start.targetX - w.x) >= 0.15, 'target too close to start');
  assert.ok(start.targetX >= -2 && start.targetX <= 2, 'target outside bounds');
  assert.equal(Math.sign(start.targetX - start.dir * 0), Math.sign(start.dir) || 1);
});

test('never walks while listening/thinking/speaking/typing/approval/dragging', () => {
  for (const busy of ['listening', 'hearing', 'thinking', 'speaking']) {
    const w = madeWalker();
    w.armIdle(0);
    w.status(0.5, busy);
    let acts = [];
    for (let t = 0; t <= 30; t += 0.1) acts.push(...w.tick(t, 0.1));
    assert.equal(acts.find((a) => a.type === 'walk_start'), undefined, `walked while ${busy}`);
  }
  for (const kind of ['approval', 'typing', 'dragging']) {
    const w = madeWalker();
    w.armIdle(0);
    w.setBlocked(0.5, kind, true);
    let acts = [];
    for (let t = 0; t <= 30; t += 0.1) acts.push(...w.tick(t, 0.1));
    assert.equal(acts.find((a) => a.type === 'walk_start'), undefined, `walked while ${kind}`);
  }
});

test('a walk in progress stops the moment a status/typing/approval starts', () => {
  const w = madeWalker();
  w.armIdle(0);
  for (let t = 0; t <= 12.1; t += 0.1) w.tick(t, 0.1);
  assert.ok(w.walking, 'walk should be running');
  const stops = w.status(w.nextRoamAt, 'thinking');       // mid-walk interruption
  assert.deepEqual(stops.map((a) => a.type), ['walk_stop']);
  assert.equal(w.walking, false);
  const x = w.x;
  for (let t = 13; t < 20; t += 0.1) w.tick(t, 0.1);      // root motion halted
  assert.equal(w.x, x);
  // typing / approval work the same way
  const w2 = madeWalker();
  w2.roamNow(0, { dir: 1, seconds: 5 });
  assert.ok(w2.walking);
  assert.equal(w2.setBlocked(1, 'typing', true)[0]?.type, 'walk_stop');
  assert.equal(w2.walking, false);
});

test('root motion: speed = 0.6 m/s x height scale x speedMul, x integrates per dt', () => {
  const w = madeWalker({ speedMul: 2 });
  assert.ok(Math.abs(w.speed - 0.6 * 2 * (1.5 / 1.5)) < 1e-9);
  const h = new WalkDirector({ every: [10, 10] });
  h.configure({ height: 1.0, speedMul: 1.0, bounds: { min: -9, max: 9 } });
  assert.ok(Math.abs(h.speed - 0.6 * (1.0 / 1.5)) < 1e-9, 'speed scales with height');
  h.roamNow(0, { dir: 1, seconds: 3 });
  for (let i = 0; i < 100; i++) h.tick(0.1 * (i + 1), 0.1);   // 10 s at 0.1 dt
  assert.ok(h.x > 0, 'she never moved');
  // integrate cleanly: run 1 s at 60 fps straight after roamNow on a fresh walker
  const h2 = new WalkDirector({ every: [10, 10] });
  h2.configure({ height: 1.5, speedMul: 1, bounds: { min: -9, max: 9 } });
  h2.roamNow(0, { dir: -1, seconds: 10 });
  for (let i = 1; i <= 600; i++) h2.tick(i / 60, 1 / 60);     // 10 s walking left
  assert.ok(Math.abs(h2.x - -0.6 * 10) < 0.05, `x drifted: ${h2.x}`);
});

test('strip edges: she turns around at the margin, never past the bounds', () => {
  const w = madeWalker();
  w.x = 1.9;
  const acts = w.roamNow(0, { dir: 1, seconds: 10 });
  assert.equal(acts[0].type, 'walk_start');
  let turns = 0;
  for (let t = 0.1; t <= 5; t += 0.1) {
    for (const a of w.tick(t, 0.1)) if (a.type === 'walk_turn') turns++;
    assert.ok(w.x <= 2.0 + 1e-9 && w.x >= -2.0 - 1e-9, `x out of bounds: ${w.x}`);
  }
  assert.ok(turns >= 1, 'no turnaround at the edge');
});

test('dragging moves her along the strip and blocks walking while held', () => {
  const w = madeWalker();
  w.roamNow(0, { dir: 1, seconds: 5 });
  const acts = w.setBlocked(0.5, 'dragging', true);
  assert.equal(acts[0].type, 'walk_stop');
  w.setX(0.6, 1.2);
  assert.equal(w.x, 1.2);
  w.setX(0.7, 99);                       // clamped to the bound
  assert.equal(w.x, 2.0);
  const before = w.x;
  for (let t = 0.8; t < 5; t += 0.1) w.tick(t, 0.1);
  assert.equal(w.x, before, 'root motion ran while dragged');
});

test('persistence round-trip: setX clamps into bounds and re-arms the schedule', () => {
  const w = madeWalker();
  w.setX(0, -1.25);
  assert.equal(w.x, -1.25);
  w.setX(0, -99);
  assert.equal(w.x, -2.0);
  w.setX(0, NaN);                        // garbage must not poison x
  assert.equal(w.x, -2.0);
  // still roams after the restore
  for (let t = 0; t <= 12.1; t += 0.1) if (w.tick(t, 0.1).some((a) => a.type === 'walk_start')) return;
  assert.fail('no roam after restore');
});

// ── Director interplay ─────────────────────────────────────────────
function withWalk(dRandom = seq(0.5), wRandom = seq(0.5), opts = {}) {
  const walk = madeWalker(opts, wRandom);
  const d = new Director({ random: dRandom, walk });
  return { d, walk };
}

test('natural gestures wait while she walks; they never interrupt it', () => {
  const { d, walk } = withWalk();
  d.status(0, 'idle');
  walk.roamNow(0.1, { dir: 1, seconds: 5 });
  assert.ok(walk.walking);
  const acts = d.say(0.2, { emotion: 'happy', seconds: 1 });  // happy -> nod gesture
  assert.ok(acts.some((a) => a.type === 'walk_stop'), 'a line stops the walk first');
  assert.ok(acts.some((a) => a.type === 'gesture' && a.name === 'nod'), 'then the gesture plays');
  // idle gestures never fire mid-walk, even when their timer is due
  const { d: d2, walk: w2 } = withWalk();
  d2.start(0);                       // startup wave (2.4 s), gesture clear by t=3
  d2.status(0.01, 'idle');
  w2.configure({ bounds: { min: -50, max: 50 } });   // long clear strip
  w2.roamNow(0.1, { dir: 1, seconds: 20 });
  d2.nextIdleAt = 3;                 // force the idle-gesture window open
  const fired = [];
  for (let t = 3; t <= 12; t += 0.1) {
    for (const a of d2.tick(t, 0.1)) if (a.type === 'gesture') fired.push(a.name);
  }
  assert.ok(w2.walking, 'walk should still be running');
  assert.deepEqual(fired, [], `gesture started mid-walk: ${fired}`);
});

test('forced W stops the walk FIRST, then waves', () => {
  const { d, walk } = withWalk();
  d.status(0, 'idle');
  for (const a of d.tick(0.0, 1 / 60)) void a;
  walk.roamNow(0.5, { dir: 1, seconds: 5 });
  const acts = d.requestWave(1.0);
  assert.equal(acts[0].type, 'walk_stop');
  assert.equal(acts[0].reason, 'forced');
  assert.ok(acts.some((a) => a.type === 'gesture' && a.name === 'wave'));
  assert.equal(walk.walking, false);
  // already-waving path also stops the walk
  walk.roamNow(2.0, { dir: 1, seconds: 5 });
  const acts2 = d.requestWave(2.5);
  assert.equal(acts2[0].type, 'walk_stop');
});

test('asked-for reply actions stop the walk first too', () => {
  const { d, walk } = withWalk();
  d.status(0, 'idle');
  walk.roamNow(0.5, { dir: 1, seconds: 5 });
  d.heard(0.6, 'wave at me');
  const acts = d.say(0.7, { seconds: 0, actions: [{ name: 'wave', at: 0, asked: true }] });
  assert.equal(acts[0].type, 'walk_stop');
  assert.ok(acts.some((a) => a.type === 'gesture' && a.name === 'wave'));
});

test('persona switch stops the walk before the greeting wave', () => {
  const { d, walk } = withWalk();
  walk.roamNow(0.5, { dir: 1, seconds: 5 });
  const acts = d.persona(1.0, { id: 'asuka', avatar_version: 2 });
  assert.equal(acts[0].type, 'walk_stop');
  assert.ok(acts.some((a) => a.type === 'gesture' && a.name === 'wave'));
});

test('status(listening) mid-walk emits walk_stop before the listen loop', () => {
  const { d, walk } = withWalk();
  d.status(0, 'idle');
  walk.roamNow(0.5, { dir: 1, seconds: 5 });
  const acts = d.status(1.0, 'listening');
  assert.equal(acts[0].type, 'walk_stop');
  assert.ok(acts.some((a) => a.type === 'gesture' && a.name === 'listen' && a.loop));
});

test('the roam schedule runs through Director.tick (page wiring)', () => {
  const { d, walk } = withWalk(seq(0.5, 0.5), seq(0.9, 0.3));
  d.start(0);
  d.status(0.01, 'idle');
  let sawStart = false;
  for (let t = 0; t <= 12.2; t += 0.1) {
    for (const a of d.tick(t, 0.1)) if (a.type === 'walk_start') sawStart = true;
  }
  assert.ok(sawStart, 'Director.tick never emitted walk_start');
  assert.ok(walk.walking || walk.x !== 0);
});

// ── the walk cycle itself ──────────────────────────────────────────
test('walkPose: every bone inside WALK_LIMITS for every phase (10-min sim)', () => {
  const cfgs = [
    { legLen: 0.7, thighSign: 1, kneeSign: 1 },     // VRM 1.0-ish sample
    { legLen: 0.82, thighSign: -1, kneeSign: -1 },  // long-legged, flipped axes
    { legLen: 0.35, thighSign: 1, kneeSign: -1 },   // tiny chibi proportions
  ];
  const dt = 1 / 60;
  for (const cfg of cfgs) {
    const perStep = cadence(0.6 * (cfg.legLen / 0.7), cfg) * 2 * Math.PI;
    let phase = 0;
    // 10 simulated minutes, slightly varying speed (walk + turn moments)
    for (let f = 0; f < 600 * 60; f++) {
      phase += perStep * dt * (1 + 0.05 * Math.sin(f * 0.001));
      const { bones, bob } = walkPose(phase, cfg);
      for (const bone of WALK_BONES) {
        const rot = bones[bone];
        assert.ok(rot, `missing ${bone}`);
        for (const v of rot) assert.ok(Number.isFinite(v), `${bone} not finite`);
        const lim = WALK_LIMITS[bone];
        for (const v of rot) {
          assert.ok(Math.abs(v) <= lim + 1e-9,
            `${bone} ${JSON.stringify(rot)} exceeds ${lim} (legLen ${cfg.legLen})`);
        }
      }
      // the knee only bends one way (never hyperextends backwards)
      for (const s of ['left', 'right']) {
        const knee = bones[`${s}LowerLeg`][0] * cfg.kneeSign;
        assert.ok(knee >= -1e-9 && knee <= kneeMax(cfg.legLen) + 1e-9,
          `${s}LowerLeg bend ${knee} out of [0, ${kneeMax(cfg.legLen)}]`);
      }
      assert.ok(Math.abs(bob) <= 0.05 * cfg.legLen + 1e-9, `hips bob too big: ${bob}`);
    }
  }
});

test('walk cycle is proportion-aware: bigger legs -> bigger stride', () => {
  const short = { legLen: 0.35, thighSign: 1, kneeSign: 1 };
  const long = { legLen: 0.9, thighSign: 1, kneeSign: 1 };
  assert.ok(strideLen(long) > 2 * strideLen(short), 'stride must scale with legs');
  // a given speed means a smaller/Slower gait on long legs, faster on short
  assert.ok(cadence(0.6, long) < cadence(0.6, short));
  const envShort = walkPoseEnvelope(short), envLong = walkPoseEnvelope(long);
  // stride scales linearly with leg length at the (size-independent) angle
  assert.ok(Math.abs(strideLen(long) / strideLen(short) - long.legLen / short.legLen) < 1e-9,
    'stride must scale with leg length');
  // amplitude never exceeds the envelope of limits for any size
  for (const [bone, m] of Object.entries(envLong)) {
    assert.ok(m <= WALK_LIMITS[bone] + 1e-9, `${bone} envelope ${m} > ${WALK_LIMITS[bone]}`);
  }
  void thighAmp;
});
