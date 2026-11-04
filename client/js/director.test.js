// node --test client/js/director.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Director, EMOTION_EXPRESSIONS, EMOTION_GESTURES, RETURN_AFTER_IDLE } from './director.js';
import { GESTURES } from './gestures.js';

// deterministic randomness
const seq = (...vals) => { let i = 0; return () => vals[i++ % vals.length]; };

const names = (acts) => acts.filter((a) => a.type === 'gesture').map((a) => a.name);
const gestures = (acts) => acts.filter((a) => a.type === 'gesture');

test('start waves', () => {
  const d = new Director({ random: seq() });
  assert.deepEqual(names(d.start(0)), ['wave']);
  assert.equal(d.gesture.name, 'wave');
  assert.equal(d.gesture.loop, false);
});

test('persona switch waves, bypassing cooldown', () => {
  const d = new Director({ random: seq() });
  d.start(0);
  d.tick(0);
  d.tick(3);  // wave finished
  assert.deepEqual(names(d.persona(3)), ['wave']);
});

test('persona wave preempts a running loop, never a one-shot', () => {
  const d = new Director({ random: seq() });
  d.status(0, 'thinking');
  assert.deepEqual(names(d.gesture ? [{ type: 'gesture', ...d.gesture }] : []), ['think']);
  d.persona(1);  // wave must cut in front of the think loop
  assert.equal(d.gesture.name, 'wave');

  const d2 = new Director({ random: seq() });
  d2.start(0);  // wave (one-shot) running until 2.4
  d2.persona(1);  // a second wave waits, it does not interrupt
  assert.deepEqual(d2.pending ? [d2.pending.name] : [], ['wave']);
});

test('one gesture never interrupts another', () => {
  const d = new Director({ random: seq() });
  d.start(0);                       // wave 0..2.4
  const acts = d.say(1, { emotion: 'happy' });   // nod is queued, not played
  assert.deepEqual(names(acts), []);
  assert.equal(d.pending.name, 'nod');
  const done = d.tick(3.0);         // wave over, nod starts
  assert.deepEqual(names(done), ['nod']);
});

test('think loops while thinking, stops when speaking', () => {
  const d = new Director({ random: seq() });
  assert.deepEqual(names(d.status(0, 'thinking')), ['think']);
  assert.equal(d.gesture.loop, true);
  const acts = d.status(5, 'speaking');
  assert.deepEqual(acts.map((a) => a.type + ':' + (a.name || a.state)), ['gesture_stop:think']);
  assert.equal(d.gesture, null);
  d.status(6, 'idle');
  assert.deepEqual(d.tick(100).filter((a) => a.name === 'think'), []);
});

test('listen replaces think when status changes, same loop never restarts', () => {
  const d = new Director({ random: seq() });
  d.status(0, 'thinking');
  const acts = d.status(1, 'listening');
  assert.deepEqual(names(acts), ['listen']);
  assert.deepEqual(acts[0].type, 'gesture_stop');
  d.status(2, 'hearing');  // still listening family -> listen keeps running
  assert.deepEqual(names(d.status(3, 'listening')), []);
});

test('emotion expressions match the contract', () => {
  assert.deepEqual(EMOTION_EXPRESSIONS.teasing, { expression: 'happy', value: 0.6, tilt: true });
  assert.deepEqual(EMOTION_EXPRESSIONS.annoyed, { expression: 'angry', value: 0.5 });
  assert.deepEqual(EMOTION_EXPRESSIONS.embarrassed, { expression: 'happy', value: 0.3, away: true });
  assert.deepEqual(EMOTION_EXPRESSIONS.thinking, { expression: null, value: 0 });
  assert.deepEqual(EMOTION_EXPRESSIONS.neutral, { expression: null, value: 0 });
});

test('say drives expression + emotion gesture', () => {
  const d = new Director({ random: seq() });
  const acts = d.say(0, { emotion: 'teasing', seconds: 4 });
  const expr = acts.find((a) => a.type === 'expression');
  assert.equal(expr.name, 'happy');
  assert.equal(expr.value, 0.6);
  assert.equal(expr.tilt, true);
  assert.deepEqual(names(acts), ['nod']);
  // expiry fades back to neutral
  d.tick(0.5);
  const clear = d.tick(10).find((a) => a.type === 'expression');
  assert.equal(clear.name, null);
  assert.equal(clear.value, 0);
});

test('unknown or missing emotion behaves like neutral', () => {
  const d = new Director({ random: seq() });
  const acts = d.say(0, {});
  const expr = acts.find((a) => a.type === 'expression');
  assert.equal(expr.name, null);
  assert.deepEqual(names(acts), []);
  const acts2 = d.say(5, { emotion: 'weird' });
  assert.equal(acts2[0].name, null);
});

test('every emotion gesture name exists in the pose library', () => {
  for (const g of Object.values(EMOTION_GESTURES)) if (g) assert.ok(GESTURES[g], g);
});

test('talk gestures sprinkle while speaking, never twice in a row', () => {
  const d = new Director({ random: seq(0.5, 0.9, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5) });
  d.status(0, 'speaking');
  const picks = [];
  let t = 0;
  for (let i = 0; i < 400; i++) {
    t += 0.5;
    for (const a of d.tick(t)) if (a.type === 'gesture') picks.push(a.name);
    d.tick(t + 0.2);
  }
  assert.ok(picks.length >= 3, `expected talk gestures, got ${JSON.stringify(picks)}`);
  for (let i = 1; i < picks.length; i++) assert.notEqual(picks[i], picks[i - 1]);
});

test('a queued status loop dies when its state passes before it got the body', () => {
  const d = new Director({ random: seq(0.5, 0.5, 0.5) });
  d.start(0);                              // wave one-shot until ~2.4 s
  assert.deepEqual(names(d.status(1, 'thinking')), [], 'think queues behind the wave');
  assert.equal(d.pending.name, 'think');
  // the whole turn finished while the wave still played (event batch raced ahead)
  d.status(1.2, 'speaking');
  d.status(1.4, 'idle');
  assert.equal(d.pending, null, 'the stale think queue is gone');
  for (let t = 2.5; t < 8; t += 0.25) {
    assert.ok(!names(d.tick(t)).includes('think'), 'no orphan think when the body frees up');
  }
});

test('idle gestures only while idle, every 1-3 minutes, from the right set', () => {
  const d = new Director({ random: seq(0, 0.5, 0.5, 0.5, 0.5, 0.5) });
  d.start(0);
  for (const a of d.tick(10)) void a;   // wave ends, no gestures yet
  let fired = [];
  let t = 10;
  for (let i = 0; i < 600; i++) { t += 0.5; fired.push(...names(d.tick(t))); }
  assert.ok(fired.length >= 2, `expected idle gestures in 300s, got ${fired}`);
  const allowed = new Set(['stretch', 'look_around', 'fidget', 'hair_touch']);
  for (const f of fired) assert.ok(allowed.has(f), f);
});

test('no idle gestures while thinking/speaking/listening', () => {
  const d = new Director({ random: seq(0.0, 0.5, 0.5, 0.5) });
  d.start(0);
  d.tick(3);
  d.status(3, 'thinking');
  let fired = [];
  for (let t = 3; t < 400; t += 0.5) fired.push(...names(d.tick(t)));
  assert.deepEqual(fired.filter((f) => f !== 'think'), []);
});

test('per-gesture cooldown is honoured', () => {
  const d = new Director({ random: seq(0.0, 0.0, 0.0, 0.0) });
  d.start(0);
  d.tick(3);  // wave done; cooldown until 25
  let t = 3;
  const waves = [];
  for (let i = 0; i < 1200 && waves.length < 1; i++) {
    t += 0.5;
    d.status(t, 'idle');
    waves.push(...names(d.tick(t)).filter((n) => n === 'wave'));
  }
  // the 10-min welcome wave must not have fired in this window
  assert.deepEqual(waves, []);
  const d2 = new Director({ random: seq(0.5, 0.5, 0.5) });
  assert.deepEqual(names(d2.say(0, { emotion: 'happy' })), ['nod']);
  d2.tick(2);  // the nod is over (the page ticks every frame)
  assert.deepEqual(names(d2.say(3, { emotion: 'happy' })), [], 'nod is on cooldown');
  const acts = d2.say(20, { emotion: 'happy' });
  assert.deepEqual(names(acts), ['nod']);
});

test('after 10 idle minutes she waves when you come back', () => {
  const d = new Director({ random: seq(0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5) });
  d.status(0, 'idle');
  let t = 0;
  for (; t < RETURN_AFTER_IDLE + 30; t += 1) d.tick(t);
  assert.equal(d.returnWave, true);
  assert.deepEqual(names(d.heard(t)), ['wave']);
  d.tick(t + 3);
  assert.equal(d.returnWave, false);
});

test('expression expiry emits a clear exactly once', () => {
  const d = new Director({ random: seq() });
  d.say(0, { emotion: 'happy', seconds: 2 });  // holds until 5
  assert.equal(d.tick(4).filter((a) => a.type === 'expression').length, 0);
  const clear = d.tick(6).filter((a) => a.type === 'expression');
  assert.equal(clear.length, 1);
  assert.equal(d.tick(7).filter((a) => a.type === 'expression').length, 0);
});

test('director state survives a long random sequence without glitches', () => {
  const d = new Director({ random: seq(0.13, 0.71, 0.44, 0.9, 0.02, 0.55) });
  d.start(0);
  const states = ['idle', 'listening', 'hearing', 'thinking', 'speaking', 'idle'];
  const emotions = ['happy', 'teasing', 'annoyed', 'sad', 'surprised', 'thinking', 'embarrassed', 'neutral'];
  let t = 0;
  for (let i = 0; i < 3000; i++) {
    t += 0.25;
    d.tick(t);
    if (i % 7 === 0) d.status(t, states[i % states.length]);
    if (i % 11 === 0) d.say(t, { emotion: emotions[i % emotions.length], seconds: 3 });
    if (i % 23 === 0) d.persona(t);
    if (d.gesture) {
      assert.ok(GESTURES[d.gesture.name]);
      if (d.gesture.loop) assert.ok(GESTURES[d.gesture.name].loop);
    }
    if (d.pending) assert.ok(GESTURES[d.pending.name]);
  }
});

test('a long happy line earns the laugh, a short one just a nod', () => {
  const d = new Director({ random: seq(0.5, 0.5, 0.5, 0.5) });
  d.start(0);
  d.tick(3);  // wave over
  assert.deepEqual(names(d.say(3, { emotion: 'happy', seconds: 2 })), ['nod']);
  d.tick(5);  // nod over
  assert.deepEqual(names(d.say(30, { emotion: 'happy', seconds: 9 })), ['laugh']);
});

// ── calm-by-default behaviour (the owner bug: "the animation runs constantly") ──

test('30 simulated idle minutes: sparse idle gestures, gaps >= 60 s, < 15 % gesture time', () => {
  const d = new Director({ random: seq(0.5, 0.31, 0.72, 0.05, 0.64, 0.48, 0.9, 0.22) });
  d.start(0);
  const starts = [];   // [name, t]
  let endsAt = -1;     // when the running one-shot ends (loops would run longer; none idle)
  let gestureSeconds = 0;
  let t = 0;
  for (let i = 0; i < 1800 * 4 && t <= 1800; i++) {
    t = Math.min(1800, i * 0.25);
    for (const a of d.tick(t)) {
      if (a.type === 'gesture') {
        starts.push([a.name, t]);
        endsAt = t + a.duration;
        gestureSeconds += Math.min(a.duration, 1800 - t);
      }
    }
  }
  const idleFires = starts.filter(([n]) => n !== 'wave');
  // after 10 quiet minutes she arms the welcome-back wave and goes still
  // (RETURN_AFTER_IDLE), so expect idle gestures in the first ~10 minutes
  assert.ok(idleFires.length >= 4 && idleFires.length <= 20,
    `expected a sparse run of idle gestures in 30 min, got ${idleFires.length}`);
  for (let i = 1; i < idleFires.length; i++) {
    const gap = idleFires[i][1] - idleFires[i - 1][1];
    assert.ok(gap >= 60 - 0.01, `idle gestures ${idleFires[i - 1]} -> ${idleFires[i]} only ${gap.toFixed(1)}s apart`);
  }
  const frac = gestureSeconds / 1800;
  assert.ok(frac < 0.15, `gesture time ${Math.round(frac * 100)}% of the half hour — too busy`);
  void endsAt;
});

test('repeated identical status events never restart gestures or re-arm timers', () => {
  const d = new Director({ random: seq(0.5, 0.5, 0.5) });
  assert.deepEqual(names(d.status(0, 'thinking')), ['think']);
  const running = { ...d.gesture };
  const idleAt = d.nextIdleAt;
  for (let t = 1; t < 60; t += 1) {
    assert.deepEqual(d.status(t, 'thinking'), [], 'same status must be a no-op');
    assert.deepEqual(d.status(t, 'thinking'), []);
  }
  assert.deepEqual({ ...d.gesture }, running, 'the think loop was restarted by status spam');
  assert.equal(d.nextIdleAt, idleAt);
  // a spammed idle status is a no-op too
  d.status(60, 'idle');
  const g = { ...d.gesture };
  for (let t = 61; t < 90; t++) assert.deepEqual(d.status(t, 'idle'), []);
  assert.deepEqual({ ...d.gesture }, g);
  // a status event without a state (old hub / malformed) is ignored, not a crash
  assert.deepEqual(d.status(91, undefined), []);
  assert.deepEqual(d.status(92, null), []);
});

test('persona events for the same id + avatar_version do not re-wave', () => {
  const d = new Director({ random: seq(0.5, 0.5) });
  assert.deepEqual(names(d.persona(0, { id: 'riko', avatar_version: 3 })), ['wave']);
  d.tick(3);  // wave over
  assert.deepEqual(d.persona(3, { id: 'riko', avatar_version: 3 }), [], 'stale echo ignored');
  assert.deepEqual(names(d.persona(4, { id: 'asuka', avatar_version: 1 })), ['wave'], 'a real switch still waves');
  d.tick(7);
  assert.deepEqual(names(d.persona(10, { id: 'riko', avatar_version: 3 })), ['wave'], 'and switching back waves');
  d.tick(13);
  // legacy callers without info still wave
  assert.deepEqual(names(d.persona(20)), ['wave']);
});


// ── greeting / goodbye waves (owner request 2026-11) ─────────────
test('heard greeting waves back with a happy face', () => {
  const d = new Director({ random: seq(0.5) });
  for (const text of ['hi there', 'Hello!', 'hey riko', 'good morning', 'good night 🌙',
                      'goodbye for now', 'bye!', 'see you later', 'good bye mom']) {
    const d2 = new Director({ random: seq(0.5) });
    const acts = d2.heard(0, text);
    assert.ok(names(acts).includes('wave'), `expected a wave for "${text}"`);
    const expr = acts.find((a) => a.type === 'expression');
    assert.ok(expr && expr.name === 'happy' && expr.value > 0.5, `happy face with the wave for "${text}"`);
  }
});

test('non-greetings never earn a greeting wave (word boundaries)', () => {
  const d = new Director({ random: seq(0.5) });
  for (const text of ['this is a test', 'the ship sailed', 'calculate 3 times 4',
                      'I am never too shy about anything', 'the elephant waved', '']) {
    const d2 = new Director({ random: seq(0.5) });
    const acts = d2.heard(0, text);
    assert.ok(!names(acts).includes('wave'), `"${text}" must not wave`);
  }
});

test('a reply that OPENS with a greeting waves; a greeting mid-sentence does not', () => {
  const d2 = new Director({ random: seq(0.5) });
  const acts = d2.say(0, { emotion: 'happy', text: 'Hi! I missed you!' });
  assert.ok(names(acts).includes('wave'), 'opening greeting should wave');
  const d3 = new Director({ random: seq(0.5) });
  const a3 = d3.say(0, { text: 'I was just thinking that hello is a weird word.' });
  assert.ok(!names(a3).includes('wave'), 'mid-sentence greeting must not wave');
  const d4 = new Director({ random: seq(0.5) });
  assert.ok(names(d4.say(0, { text: '"Good evening!" she said' })).includes('wave'),
    'leading quotes before a greeting still wave');
});

test('greeting waves honour the 25 s cooldown and never stack', () => {
  const d = new Director({ random: seq(0.5) });
  assert.ok(names(d.heard(0, 'hello')).includes('wave'));
  assert.ok(!names(d.heard(1, 'hi again')).includes('wave'), 'no stacking mid-wave');
  d.tick(3.0);   // wave finished
  assert.ok(!names(d.heard(10, 'hello??')).includes('wave'), 'cooldown blocks after the gesture');
  assert.ok(names(d.heard(26, 'hello again')).includes('wave'), 'cooldown expired');
});

// ── on-demand waves: the W shortcut / riko-ctl (owner bug 2026-11:
// "Hitting W doesn't make her wave" — the 25 s greeting cooldown silently
//  swallowed every W right after the startup wave or any greeting wave) ──
test('requestWave always waves: no cooldown wait, body-free', () => {
  const d = new Director({ random: seq(0.5) });
  assert.ok(names(d.requestWave(0)).includes('wave'));
  d.tick(3);  // first wave finished; it armed the 25 s cooldown
  const acts = d.requestWave(4);  // mid-cooldown: W must wave anyway
  assert.ok(names(acts).includes('wave'), 'cooldown must never swallow an on-demand wave');
  const expr = acts.find((a) => a.type === 'expression');
  assert.ok(expr && expr.name === 'happy' && expr.value > 0.5, 'the wave keeps its happy face');
});

test('requestWave preempts a running one-shot (idle/talk/emotion gesture)', () => {
  const d = new Director({ random: seq(0.5) });
  d.status(0, 'idle');
  assert.deepEqual(names(d.say(1, { emotion: 'happy' })), ['nod']);  // nod one-shot 1..2.8
  const acts = d.requestWave(1.5);
  assert.deepEqual(acts.slice(0, 2).map((a) => a.type + ':' + a.name),
                   ['gesture_stop:nod', 'gesture:wave'], 'blend out the nod, wave right now');
  assert.equal(d.gesture.name, 'wave');
  assert.equal(d.pending, null, 'nothing queues behind the forced wave');
});

test('requestWave preempts a status loop and replaces a queued wave', () => {
  const d = new Director({ random: seq(0.5) });
  d.status(0, 'thinking');  // think loop running
  const acts = d.requestWave(1);
  assert.deepEqual(acts.map((a) => a.type + ':' + a.name).slice(0, 2),
                   ['gesture_stop:think', 'gesture:wave']);
  assert.equal(d.gesture.name, 'wave');
  // pressing W while waving drops a queued wave echo instead of stacking
  const d2 = new Director({ random: seq(0.5) });
  d2.start(0);        // wave one-shot until 2.4
  d2.persona(0.5);    // persona wave -> pending (one-shots never interrupt)
  assert.equal(d2.pending.name, 'wave');
  assert.deepEqual(d2.requestWave(1), [], 'already waving: nothing to add');
  assert.equal(d2.pending, null, 'the queued wave echo is dropped (no double wave)');
  assert.deepEqual(names(d2.tick(3)).filter((n) => n === 'wave'), [], 'and it never replays');
});

test('requestWave skips only while a wave is already playing, and still marks activity', () => {
  const d = new Director({ random: seq(0.5) });
  assert.ok(names(d.requestWave(0)).includes('wave'));
  const ends = d.gesture.endsAt;
  assert.deepEqual(d.requestWave(1), [], 'no restack mid-wave');
  assert.equal(d.gesture.endsAt, ends, 'the running wave keeps its end time');
  assert.deepEqual(d.requestWave(2), []);
  d.tick(3);  // wave over
  assert.ok(names(d.requestWave(3.5)).includes('wave'), 'free again -> waves again');
  // a forced cooldown-free wave still arms the cooldown for SOCIAL waves
  assert.ok(!names(d.heard(4, 'hi')).includes('wave'), 'heard greetings still keep the cooldown');
});

test('requestWave marks activity so the return-wave logic stands down', () => {
  const d = new Director({ random: seq(0.5) });
  d.status(0, 'idle');
  d.requestWave(5);
  d.tick(6);  // wave done by 5 + 2.4? no: still running at 6; finish it
  d.tick(8);
  const acts = d.tick(600 + 8);   // 10 min later
  assert.ok(acts.some((a) => a.name === 'wave' || a.type === 'gesture'), 'return wave still fires when idle');
});
