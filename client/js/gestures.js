// Gesture pose library + small pure math used by the avatar page.
//
// No imports here at all: this module runs unchanged in the browser, in
// `node --test`, and in any headless harness.
//
// Pose space
// ──────────
// Every pose is a map of VRM *normalized humanoid* bone name -> [x, y, z]
// Euler rotation in radians, written for VRM 1.0 rigs (the values were
// probed against character_files/sample_avatar.vrm in a headless browser,
// so hands really land on the chin, overhead, etc). Values are the FULL
// local rotation including the arms-down rest (leftUpperArm.z = -1.3 /
// rightUpperArm.z = +1.3, matching the rest pose index.html sets in
// attach()). Missing bones/axes mean the rest rotation.
//
// Euler poses encode ONE rig's bone axes and limb lengths. On other models
// (the owner's VRM 0.x 'Furina' avatar: longer arms, different axes, no
// chest bone) the same numbers put the arms anywhere. So the two signature
// gestures that must always read correctly — wave and think — carry an
// `arms` block instead: world-space WRIST target tracks solved per-rig by the
// 2-bone IK in ik.js (see its header for the rig-profile and target-spec
// format). Their `keys` only hold the bones that transfer fine across rigs
// (neck, spine, shoulders, hand/finger curls); the upperArm/lowerArm eulers
// that used to live there are gone.
//
// For VRM 0.x rigs the player mirrors the sign of every upperArm z, the
// same convention attach() already uses to lower the arms (best effort:
// VRM 0.x rigs are mirrored on this axis; other axes vary per rig). IK-driven
// arms need no mirroring — the solver reads the rig's own geometry.
//
// Keys are [time, bones] pairs; the player interpolates between them with
// smoothstep easing (gestures ease in and out of the rest pose on their
// own, no separate blend needed for the built-ins).

/** Bone names used by the poses (missing in a given VRM are skipped). */
export const POSE_BONES = [
  'hips', 'spine', 'chest', 'upperChest', 'neck',
  'leftShoulder', 'leftUpperArm', 'leftLowerArm', 'leftHand',
  'rightShoulder', 'rightUpperArm', 'rightLowerArm', 'rightHand',
  // right-hand fingers (the think pose curls them onto the chin; rigs
  // without finger bones just keep an open hand, nothing else breaks)
  'rightThumbProximal',
  'rightIndexProximal', 'rightIndexIntermediate',
  'rightMiddleProximal', 'rightMiddleIntermediate',
  'rightRingProximal', 'rightRingIntermediate',
  'rightLittleProximal', 'rightLittleIntermediate',
];

const REST_L_UPPER = -1.3;
const REST_R_UPPER = 1.3;

/** Curled right hand (hand-on-chin): proximal + intermediate joints bent,
 *  thumb resting on the jaw. `c` scales the curl (finger taps vary it). */
function curlRightHand(c = 1) {
  const bend = (v) => [0, v * c, 0];
  return {
    rightThumbProximal: bend(0.55),
    rightIndexProximal: bend(0.95), rightIndexIntermediate: bend(1.05),
    rightMiddleProximal: bend(1.0), rightMiddleIntermediate: bend(1.1),
    rightRingProximal: bend(0.95), rightRingIntermediate: bend(1.0),
    rightLittleProximal: bend(0.85), rightLittleIntermediate: bend(0.9),
  };
}

// name -> { loop, duration (s), keys: [{t, bones}], osc?: {bone, axis, base, amp, freq, from, until} | osc[] }
// axis index into the [x,y,z] triplets below.
export const GESTURES = {
  // Greeting: right arm up beside the head, wrist at about head-top height
  // and ~0.65 arm-lengths out from the centre line, palm-ish to the camera,
  // wrist flapping (Euler osc on rightHand) while the whole arm rocks
  // side-to-side (osc on the IK target, ±0.16 arm lengths). Every number is
  // relative to THIS rig (head top / arm length), so short- and long-armed
  // models wave at the same relative height — that is the whole point of the
  // IK retarget. Elbow pole keeps the elbow low/out/back, away from the head.
  wave: {
    loop: false, duration: 2.4,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.45, bones: { neck: [0.05, 0, 0.1] } },
      { t: 1.9, bones: { neck: [0.05, 0, 0.1] } },
      { t: 2.4, bones: {} },
    ],
    osc: [
      { bone: 'rightHand', axis: 2, base: 0.35, amp: 0.6, freq: 1.5, from: 0.5, until: 2.05 },
    ],
    arms: {
      right: {
        keys: [
          { t: 0.0 },  // rest (arm down)
          { t: 0.45, target: { ref: 'headTop', unit: 'arm', off: [0.62, 0.14, 0.20] },
            pole: [-0.55, -0.72, -0.35] },
          { t: 1.9, target: { ref: 'headTop', unit: 'arm', off: [0.62, 0.14, 0.20] },
            pole: [-0.55, -0.72, -0.35] },
          { t: 2.4 },  // back to rest
        ],
        osc: [{ axis: 0, amp: 0.16, freq: 1.5, from: 0.5, until: 2.05 }],
      },
    },
  },

  // Classic thinker, defined in rig-relative space (see ik.js): the right
  // wrist IK-targets just below-and-right of the chin (a quarter of a head
  // length away, so the curled fingers touch it), the left wrist cups the
  // RIGHT elbow (solved first), elbows hang low and forward at the ribs,
  // right hand curled + head micro-tilt stay Euler (they transfer fine).
  // motion.js biases the gaze up-and-aside while this loops.
  think: {
    loop: true, duration: 4.0,
    keys: [
      { t: 0.0, bones: {
          rightShoulder: [-0.12, -0.45, 0.09], rightHand: [1.26, 0.16, 1.21],
          ...curlRightHand(1),
          leftShoulder: [-0.11, -0.28, 0],
          neck: [0.05, -0.08, 0.12], spine: [0.02, 0, 0] } },
      { t: 2.0, bones: {
          rightShoulder: [-0.12, -0.45, 0.09], rightHand: [1.26, 0.16, 1.21],
          ...curlRightHand(0.7),   // slow finger half-tap
          leftShoulder: [-0.11, -0.28, 0],
          neck: [0.07, -0.1, 0.14], spine: [0.03, 0, 0.01] } },  // head micro-tilt
      { t: 4.0, bones: {
          rightShoulder: [-0.12, -0.45, 0.09], rightHand: [1.26, 0.16, 1.21],
          ...curlRightHand(1),
          leftShoulder: [-0.11, -0.28, 0],
          neck: [0.05, -0.08, 0.12], spine: [0.02, 0, 0] } },
    ],
    arms: {
      right: {
        keys: [
          { t: 0.0, target: { ref: 'chin', unit: 'head', off: [0.16, -0.14, 0.16] },
            pole: [-0.55, -0.80, 0.20] },
          { t: 4.0, target: { ref: 'chin', unit: 'head', off: [0.16, -0.14, 0.16] },
            pole: [-0.55, -0.80, 0.20] },
        ],
      },
      left: {
        // cups the right elbow (resolved after the right arm solves) from
        // below-front; palm-up support of the thinker tripod. Stay close to
        // the elbow — a far target would demand a cross-body reach beyond the
        // rig's own arm length.
        keys: [
          { t: 0.0, target: { ref: 'otherElbow', unit: 'head', off: [0.04, -0.16, 0.16] },
            pole: [0.55, -0.80, 0.25] },
          { t: 4.0, target: { ref: 'otherElbow', unit: 'head', off: [0.04, -0.16, 0.16] },
            pole: [0.55, -0.80, 0.25] },
        ],
      },
    },
  },

  // Lean in and tilt the head while listening.
  listen: {
    loop: true, duration: 3.5,
    keys: [
      { t: 0.0, bones: { spine: [0.08, 0, 0], upperChest: [0.04, 0, 0], neck: [0.04, 0, 0.05] } },
      { t: 1.75, bones: { spine: [0.06, 0, 0], upperChest: [0.03, 0, 0], neck: [0.02, 0, 0.09] } },
      { t: 3.5, bones: { spine: [0.08, 0, 0], upperChest: [0.04, 0, 0], neck: [0.04, 0, 0.05] } },
    ],
  },

  // Open-hand gesture while speaking: forearm out, palm up.
  talk_1: {
    loop: false, duration: 1.8,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.5, bones: { rightUpperArm: [-0.5, 0, 0.95], rightLowerArm: [0, 1.1, 0],
                         rightHand: [0, 0, -0.3] } },
      { t: 1.1, bones: { rightUpperArm: [-0.5, 0, 0.95], rightLowerArm: [0, 1.1, 0],
                         rightHand: [0, 0, -0.3] } },
      { t: 1.8, bones: {} },
    ],
  },

  // Shrug while speaking.
  talk_2: {
    loop: false, duration: 2.0,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.6, bones: { rightUpperArm: [-0.3, 0, 0.9], leftUpperArm: [-0.3, 0, -0.9],
                         rightLowerArm: [0, 1.2, 0], leftLowerArm: [0, -1.2, 0],
                         rightShoulder: [0, 0, -0.2], leftShoulder: [0, 0, 0.2] } },
      { t: 1.3, bones: { rightUpperArm: [-0.3, 0, 0.9], leftUpperArm: [-0.3, 0, -0.9],
                         rightLowerArm: [0, 1.2, 0], leftLowerArm: [0, -1.2, 0],
                         rightShoulder: [0, 0, -0.2], leftShoulder: [0, 0, 0.2] } },
      { t: 2.0, bones: {} },
    ],
  },

  // A small point while speaking.
  talk_3: {
    loop: false, duration: 2.0,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.6, bones: { rightUpperArm: [-0.9, 0, 1.15], rightLowerArm: [0, 0.5, 0] } },
      { t: 1.4, bones: { rightUpperArm: [-0.9, 0, 1.15], rightLowerArm: [0, 0.5, 0] } },
      { t: 2.0, bones: {} },
    ],
  },

  // Idle: big stretch, arms overhead, lean back.
  stretch: {
    loop: false, duration: 3.6,
    keys: [
      { t: 0.0, bones: {} },
      { t: 1.2, bones: { rightUpperArm: [0, 0, -1.0], leftUpperArm: [0, 0, 1.0],
                         rightLowerArm: [0, 0.1, 0], leftLowerArm: [0, -0.1, 0],
                         spine: [-0.1, 0, 0], neck: [-0.2, 0, 0] } },
      { t: 2.3, bones: { rightUpperArm: [0, 0, -1.0], leftUpperArm: [0, 0, 1.0],
                         rightLowerArm: [0, 0.1, 0], leftLowerArm: [0, -0.1, 0],
                         spine: [-0.1, 0, 0], neck: [-0.2, 0, 0] } },
      { t: 3.6, bones: {} },
    ],
  },

  // Idle: slow look around (the gaze layer widens its sweeps while this runs).
  look_around: {
    loop: false, duration: 4.0,
    keys: [
      { t: 0.0, bones: {} },
      { t: 1.0, bones: { hips: [0, 0.35, 0], neck: [0.05, 0.15, 0] } },
      { t: 2.6, bones: { hips: [0, -0.3, 0], neck: [0.05, -0.12, 0] } },
      { t: 4.0, bones: {} },
    ],
  },

  // Idle: small fidget, weight shifting.
  fidget: {
    loop: false, duration: 4.5,
    keys: [
      { t: 0.0, bones: {} },
      { t: 1.0, bones: { hips: [0, 0, 0.03], spine: [0.01, 0, 0.04], neck: [0, 0, -0.05] } },
      { t: 2.2, bones: { hips: [0, 0.08, -0.02], spine: [-0.01, 0.05, -0.03], neck: [0, -0.06, 0.04] } },
      { t: 3.4, bones: { hips: [0, -0.06, 0.02], spine: [0.01, -0.04, 0.03], neck: [0, 0.05, -0.03] } },
      { t: 4.5, bones: {} },
    ],
  },

  // Idle: hand up to brush the hair back.
  hair_touch: {
    loop: false, duration: 3.4,
    keys: [
      { t: 0.0, bones: {} },
      { t: 1.0, bones: { leftUpperArm: [-0.45, 0, -1.75], leftLowerArm: [0, -2.0, 0],
                         neck: [0.05, 0, 0.06] } },
      { t: 2.2, bones: { leftUpperArm: [-0.5, 0, -1.75], leftLowerArm: [0, -2.05, 0],
                         neck: [0.05, 0, 0.08] } },
      { t: 3.4, bones: {} },
    ],
  },

  // Emotion: yes. (Neck does the work; the head bone belongs to the gaze.)
  nod: {
    loop: false, duration: 1.1,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.25, bones: { neck: [0.22, 0, 0] } },
      { t: 0.55, bones: { neck: [-0.08, 0, 0] } },
      { t: 0.8, bones: { neck: [0.15, 0, 0] } },
      { t: 1.1, bones: {} },
    ],
  },

  // Emotion: no.
  shake_head: {
    loop: false, duration: 1.4,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.25, bones: { neck: [0, 0.3, 0] } },
      { t: 0.6, bones: { neck: [0, -0.3, 0] } },
      { t: 0.95, bones: { neck: [0, 0.18, 0] } },
      { t: 1.4, bones: {} },
    ],
  },

  // Emotion: a small bouncing laugh.
  laugh: {
    loop: false, duration: 1.6,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.25, bones: { spine: [-0.04, 0, 0], neck: [-0.22, 0, 0] } },
      { t: 0.55, bones: { spine: [0.05, 0, 0], neck: [-0.1, 0, 0] } },
      { t: 0.85, bones: { spine: [-0.03, 0, 0], neck: [-0.2, 0, 0] } },
      { t: 1.15, bones: { spine: [0.04, 0, 0], neck: [-0.12, 0, 0] } },
      { t: 1.6, bones: {} },
    ],
  },

  // Emotion: pout, chin down and a little tilt.
  pout: {
    loop: false, duration: 1.8,
    keys: [
      { t: 0.0, bones: {} },
      { t: 0.4, bones: { neck: [0.18, 0, 0.09], spine: [0.03, 0, 0] } },
      { t: 1.3, bones: { neck: [0.18, 0, 0.09], spine: [0.03, 0, 0] } },
      { t: 1.8, bones: {} },
    ],
  },
};

const smoothstep = (u) => u * u * (3 - 2 * u);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/**
 * Sample a gesture pose at time `t` (seconds from the gesture's start).
 * One-shots clamp to their ends; loops wrap. Returns { bone: [x,y,z], ... }
 * with all axis values numbers (absent bones = rest).
 */
export function samplePose(name, t) {
  const g = GESTURES[name];
  if (!g) return {};
  const keys = g.keys;
  let time = t;
  if (g.loop) {
    time = ((t % g.duration) + g.duration) % g.duration;
  } else {
    time = clamp(t, 0, g.duration);
  }
  let a = keys[0], b = keys[keys.length - 1];
  for (let i = 0; i < keys.length - 1; i++) {
    if (time >= keys[i].t && time <= keys[i + 1].t) { a = keys[i]; b = keys[i + 1]; break; }
  }
  const span = b.t - a.t;
  const u = span > 0 ? smoothstep((time - a.t) / span) : 1;
  const out = {};
  for (const bone of POSE_BONES) {
    const ka = a.bones[bone], kb = b.bones[bone];
    if (!ka && !kb) continue;
    const va = ka || [0, 0, 0], vb = kb || [0, 0, 0];
    out[bone] = [0, 1, 2].map((i) => va[i] + (vb[i] - va[i]) * u);
  }
  return out;
}

/**
 * Like samplePose but with the gesture's oscillator(s) folded in (the wave's
 * wrist flap + arm rock). `osc` may be one oscillator or an array of them.
 * `t` is the same time as for samplePose.
 */
export function sampleGesture(name, t) {
  const pose = samplePose(name, t);
  const g = GESTURES[name];
  if (!g || !g.osc) return pose;
  for (const o of (Array.isArray(g.osc) ? g.osc : [g.osc])) {
    if (t < o.from || t > o.until) continue;
    // ease the wobble in and out over its window so it never snaps
    const edge = clamp(Math.min(t - o.from, o.until - t) / 0.25, 0, 1);
    const wob = Math.sin((t - o.from) * o.freq * Math.PI * 2) * o.amp * smoothstep(edge);
    const rot = pose[o.bone] || (pose[o.bone] = [0, 0, 0]);
    rot[o.axis] += (o.base || 0) * smoothstep(edge) + wob;
  }
  return pose;
}

/** VRM 0.x rigs mirror the upperArm z axis (same rule as attach() uses to lower arms). */
export function mirrorPose(pose, vrm1) {
  if (vrm1) return pose;
  const out = {};
  for (const [bone, rot] of Object.entries(pose)) {
    out[bone] = (bone === 'leftUpperArm' || bone === 'rightUpperArm')
      ? [rot[0], rot[1], -rot[2]]
      : rot.slice();
  }
  return out;
}

/** Gestures whose arms are IK-driven (name -> {arms, loop, duration}); the
 *  camera framing sweeps these to find the gesture envelope. */
export const IK_ARM_GESTURES = Object.fromEntries(
  Object.entries(GESTURES).filter(([, g]) => g.arms)
    .map(([n, g]) => [n, { arms: g.arms, loop: g.loop, duration: g.duration }]),
);

// ── Cheap vowel lip sync ────────────────────────────────────────────
//
// Given the analyser's frequency-domain data (dB, Float32Array from
// getFloatFrequencyData) and the audio context's sample rate, estimate how
// open the mouth is and which vowel (aa/ih/ou/ee/oh) fits best, from the
// spectral centroid. This is deliberately crude: it only needs to make the
// mouth *shape* change with speech, not transcribe her.
//
// Prototype centroids (Hz) of each vowel's energy, roughly from TIMIT-ish
// averages ordered by tongue height.
export const VOWEL_CENTROIDS = { ou: 350, oh: 550, aa: 800, ih: 1400, ee: 2400 };

/**
 * freqDb: array of dB magnitudes (analyser.getFloatFrequencyData output),
 * sampleRate: Hz. Returns { open, aa, ih, ou, ee, oh } in 0..1, where `open`
 * is overall loudness-driven openness and the vowels are soft weights whose
 * max the caller should raise by `open`.
 */
export function vowelWeights(freqDb, sampleRate, { silenceDb = -70 } = {}) {
  const out = { open: 0, aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
  const n = freqDb.length;
  const binHz = (sampleRate / 2) / n;
  let energy = 0, weightedHz = 0, peakDb = -Infinity;
  for (let i = 1; i < n; i++) {
    const db = freqDb[i];
    if (db <= silenceDb) continue;
    const e = Math.pow(10, db / 20);
    energy += e;
    weightedHz += e * i * binHz;
    if (db > peakDb) peakDb = db;
  }
  if (energy <= 1e-6 || peakDb <= silenceDb) return out;
  // openness: silence floor -> 0, around -18 dBFS peak -> fully open
  out.open = clamp((peakDb - silenceDb) / (-18 - silenceDb), 0, 1);
  const centroid = weightedHz / energy;
  // softmax over log-distance to the prototype centroids; the winner shapes
  // the mouth (weighted by openness), the rest go to 0 so shapes *switch*
  // instead of averaging into mush
  let best = 'aa', bestW = -1, sum = 0;
  const w = {};
  for (const [v, c] of Object.entries(VOWEL_CENTROIDS)) {
    const d = Math.log((centroid + 50) / (c + 50));
    w[v] = Math.exp(-(d * d) / 0.55);
    sum += w[v];
    if (w[v] > bestW) { bestW = w[v]; best = v; }
  }
  for (const v of Object.keys(VOWEL_CENTROIDS)) {
    out[v] = (v === best ? out.open : 0) * (sum > 0 ? 1 : 0);
  }
  return out;
}
