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
