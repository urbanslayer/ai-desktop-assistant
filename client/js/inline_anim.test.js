// node --test client/js/inline_anim.test.js
// Reply-carried inline custom animations (docs/SELF_ANIMATION.md): the live
// curtsey_twirl bug was director.say() dropping the brand-new name because
// the desktop's cache refresh lost the race against the reply.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { collectInlineAnimations, registerInlineAnimations } from './inline_anim.js';
import { Director } from './director.js';
import { validateAnimation } from './keyframes.js';

const doc = (name, over = {}) => ({
  name, description: 'a made-up dance', duration_s: 1.4, loop: false,
  ease: 'smooth',
  keyframes: [{ t: 0.0, bones: {}, expression: {} },
              { t: 1.4, bones: { hips: [0, 24, 0] }, expression: {} }],
  ...over,
});
const inline = (name, animation) => ({ type: 'gesture', custom: true,
                                       name, preview: true, animation });

test('collectInlineAnimations picks only custom actions carrying a doc', () => {
  assert.deepEqual(
    collectInlineAnimations([
      inline('curtsey_twirl', doc('curtsey_twirl')),
      { type: 'gesture', name: 'wave' },                       // built-in
      { type: 'gesture', custom: true, name: 'bow' },          // no inline
      inline('bad', 'nope'), inline('bad', ['nope']),          // junk docs
      { custom: true, animation: doc('x') },                   // no name
      null,
    ]).map((e) => e.name), ['curtsey_twirl']);
});

test('registerInlineAnimations feeds customAnims + library', () => {
  const customAnims = new Map();
  const libEntries = {};
  const api = { customAnims, validateAnimation,
                library: (extra) => Object.assign(libEntries, extra) };
  const names = registerInlineAnimations(
    [inline('curtsey_twirl', doc('curtsey_twirl'))], api);
  assert.deepEqual(names, ['curtsey_twirl']);
  assert.equal(customAnims.get('curtsey_twirl').duration_s, 1.4);
  assert.deepEqual(libEntries, { curtsey_twirl: { loop: false, duration: 1.4 } });
  // an existing list entry owns the name: the inline copy never overwrites it
  const keeper = doc('curtsey_twirl', { description: 'from the hub list' });
  customAnims.set('curtsey_twirl', keeper);
  assert.deepEqual(registerInlineAnimations(
    [inline('curtsey_twirl', doc('curtsey_twirl'))], api), []);
  assert.equal(customAnims.get('curtsey_twirl'), keeper);
});

test('registerInlineAnimations drops junk, never throws', () => {
  const customAnims = new Map();
  const api = { customAnims, validateAnimation, library: () => {} };
  assert.deepEqual(registerInlineAnimations([
    inline('broken', { name: 'broken', keyframes: 'nope' }),
    inline('short', doc('short', { duration_s: 0.05 })),      // below 0.4 s
  ], api), []);
  assert.equal(customAnims.size, 0);
  // a doc whose name field disagrees with the action is renamed, not dropped
  const renamed = doc('other_name');
  assert.deepEqual(registerInlineAnimations([inline('curtsey_twirl', renamed)], api),
                   ['curtsey_twirl']);
  assert.equal(customAnims.get('curtsey_twirl').name, 'curtsey_twirl');
});

test('say with an inline custom animation plays it: zero unknown-action warnings', () => {
  const warnings = [];
  const hooks = [];
  const origWarn = console.warn;
  console.warn = (...a) => warnings.push(a.join(' '));
  try {
    const director = new Director({ random: () => 0,
                                    onUnknownAction: (n) => hooks.push(n) });
    director.start(0);
    director.tick(3);   // welcome wave over; director is usable
    const customAnims = new Map();
    const actions = [inline('curtsey_twirl', doc('curtsey_twirl'))];
    // the page's flow: register FIRST (customAnims + director.library),
    // then hand the very same actions to director.say()
    registerInlineAnimations(actions, { customAnims, validateAnimation,
                                        library: (extra) => director.library(extra) });
    const played = director.say(100, { seconds: 2, actions });
    assert.deepEqual(played.filter((a) => a.type === 'gesture')
                           .map((a) => a.name), ['curtsey_twirl']);
    assert.equal(director.gesture.name, 'curtsey_twirl');
    assert.deepEqual(warnings, []);      // no "unknown action — ignored"
    assert.deepEqual(hooks, []);         // safety net untouched
  } finally {
    console.warn = origWarn;
  }
});

test('an unknown name still warns and fires the rescue hook exactly once per call', () => {
  const warnings = [];
  const hooks = [];
  const origWarn = console.warn;
  console.warn = (...a) => warnings.push(a.join(' '));
  try {
    const director = new Director({ random: () => 0,
                                    onUnknownAction: (n) => hooks.push(n) });
    director.start(0);
    director.tick(3);
    const played = director.say(100, { emotion: 'happy', seconds: 2,
                                       actions: [{ type: 'gesture', custom: true,
                                                   name: 'curtsey_twirl' }] });
    assert.ok(!played.some((a) => a.type === 'gesture'
                            && a.name === 'curtsey_twirl'));   // dropped (nod is the emotion default)
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /unknown action 'curtsey_twirl'/);
    assert.deepEqual(hooks, ['curtsey_twirl']);
  } finally {
    console.warn = origWarn;
  }
});

test('a throwing rescue hook never breaks say()', () => {
  const origWarn = console.warn;
  console.warn = () => {};
  try {
    const director = new Director({ random: () => 0,
                                    onUnknownAction: () => { throw new Error('boom'); } });
    director.start(0);
    director.tick(3);
    assert.doesNotThrow(() => director.say(100, { actions: [{ name: 'ghost' }] }));
  } finally {
    console.warn = origWarn;
  }
});
