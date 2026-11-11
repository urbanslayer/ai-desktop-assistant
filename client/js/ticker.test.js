// node --test js/ticker.js coverage: the song ticker's pure state policy
// (docs/SPOTIFY_NOTES.md) — show/hide, marquee decision, optimistic toggle,
// reconcile/revert, paused auto-hide, tray enable switch.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  tickerState, setEnabled, applySnapshot, press, resolveControl,
  revertPending, tickerView, PAUSED_HIDE_MS,
} from './ticker.js';

const song = (over = {}) => ({
  playing: true, uri: 'spotify:track:1', track: 'Curtsey Song',
  artists: 'Riko', album: 'Home', duration_ms: 200000, position_ms: 1000,
  ...over,
});
const seen = (s, over, seenAt) => {
  const st = applySnapshot(s, { ...song(over), _seenAt: seenAt });
  return st;
};

test('shows playing track as "track — artists", no marquee without overflow', () => {
  let s = tickerState();
  s = seen(s, {}, 1000);
  const v = tickerView(s, { overflow: false, now: 1000 });
  assert.equal(v.text, 'Curtsey Song — Riko');
  assert.equal(v.playing, true);
  assert.equal(v.marquee, false);
});

test('marquee only when the painter reports overflow', () => {
  let s = seen(tickerState(), {}, 0);
  assert.equal(tickerView(s, { overflow: true, now: 0 }).marquee, true);
  assert.equal(tickerView(s, { overflow: false, now: 0 }).marquee, false);
});

test('paused keeps the bar with the paused icon, hides after ~10 min', () => {
  let s = seen(tickerState(), { playing: false }, 1000);
  const early = tickerView(s, { now: 1000 + PAUSED_HIDE_MS - 1000 });
  assert.ok(early, 'paused bar survives until the window closes');
  assert.equal(early.playing, false);
  assert.equal(tickerView(s, { now: 1000 + PAUSED_HIDE_MS }), null);
});

test('idle snapshot (no uri) hides the ticker; so does the tray switch', () => {
  let s = seen(tickerState(), {}, 0);
  assert.ok(tickerView(s, { now: 0 }));
  assert.equal(tickerView(applySnapshot(s, { playing: false, uri: '' }), { now: 0 }), null);
  assert.equal(tickerView(setEnabled(s, false), { now: 0 }), null);
  assert.ok(tickerView(setEnabled(s, true), { now: 0 }));
});

test('missing track field hides (snapshot without a title)', () => {
  const s = applySnapshot(tickerState(), { playing: true, uri: 'x', track: '' });
  assert.equal(tickerView(s, { now: 0 }), null);
});

test('optimistic toggle: icon flips at once, truth reconciles later', () => {
  let s = seen(tickerState(), {}, 0);
  const { state, send } = press(s, 'toggle', 500);
  assert.equal(send, 'toggle');
  assert.equal(tickerView(state, { now: 500 }).playing, false, 'flips before the network');
  // hub answers with the fresh truth (paused now): the flip stands
  s = resolveControl(state, { ok: true, now_playing: song({ playing: false }) });
  assert.equal(s.pending, null);
  assert.equal(tickerView(s, { now: 600 }).playing, false);
  // second press while paused -> flips to play, then a truth that still
  // says playing reconciles to playing
  const p2 = press(s, 'toggle', 700);
  assert.equal(tickerView(p2.state, { now: 700 }).playing, true);
  s = resolveControl(p2.state, { ok: true, now_playing: song({ playing: true }) });
  assert.equal(tickerView(s, { now: 800 }).playing, true);
});

test('failed control reverts the flip and hides until the next event', () => {
  let s = seen(tickerState(), {}, 0);
  const { state } = press(s, 'toggle', 0);
  s = resolveControl(state, { ok: false, status: 409 });
  assert.equal(s.snap.playing, true, 'icon back to the truth');
  assert.equal(tickerView(s, { now: 0 }), null, 'hidden on error');
  // a later now_playing event proves things and clears the error latch
  s = applySnapshot(s, song({ playing: false }));
  const v = tickerView(s, { now: 1 });
  assert.ok(v);
  assert.equal(v.playing, false);
});

test('next: no optimistic flip, but pending until the song-change event', () => {
  let s = seen(tickerState(), {}, 0);
  const { state, send } = press(s, 'next', 0);
  assert.equal(send, 'next');
  assert.equal(tickerView(state, { now: 0 }).playing, true, 'no icon lie for next');
  s = resolveControl(state, { ok: true, now_playing: song({ uri: 'spotify:track:2', track: 'Next Song' }) });
  assert.equal(s.pending, null);
  assert.equal(tickerView(s, { now: 0 }).text, 'Next Song — Riko');
});

test('pending flip with no answer ever reverts after the timeout', () => {
  let s = seen(tickerState(), {}, 0);
  const { state } = press(s, 'toggle', 0);
  // tickerView lazily reverts once the pending flip goes stale
  const v = tickerView(state, { now: 60_000 });
  assert.equal(v, null, 'reverted to the error latch: hidden');
  assert.equal(revertPending(state).snap.playing, true, 'icon back to the pre-press truth');
});

test('press with nothing loaded sends nothing', () => {
  const { state, send } = press(tickerState(), 'toggle', 0);
  assert.equal(send, null);
  assert.equal(tickerView(state, { now: 0 }), null);
});

test('artists-only formatting: missing artists does not dangle a dash', () => {
  const s = applySnapshot(tickerState(),
    { playing: true, uri: 'u', track: 'Solo', artists: '', _seenAt: 0 });
  assert.equal(tickerView(s, { now: 0 }).text, 'Solo');
});
