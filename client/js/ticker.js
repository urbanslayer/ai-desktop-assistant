// ticker.js — pure state logic for the song ticker (docs/SPOTIFY_NOTES.md):
// a slim now-playing bar at the bottom edge with a play/pause button. No
// DOM, no timers, no fetch: every function takes state (and now) and returns
// a new state or a decision, so node --test covers the whole policy and the
// page stays a thin painter over it.
//
// State shape:
//   {
//     enabled: true,           // desktop music:ticker:{0,1} switch
//     snap: null | {playing, uri, track, artists, ...},  // hub snapshot
//     pending: null | {action, wasPlaying},  // optimistic flip awaiting
//                                             // reconcile (or revert on err)
//     error: false,            // last control failed: hide until next event
//   }
//
// Rules this module owns:
//  * show when playing or paused-with-a-track; hide when nothing/idle,
//    when disabled, when an error stuck, or ~PAUSED_HIDE_MS after a pause;
//  * text = "track — artists" (em dash, matching the owner's ask);
//  * the marquee scrolls ONLY when the text overflows (measured by the
//    painter, which feeds overflowIn/overflowOut back in — this module just
//    says whether scrolling applies at all);
//  * a press flips the icon at once (optimistic); the next now_playing (or
//    the control response) reconciles; a failed control reverts + hides.

export const PAUSED_HIDE_MS = 10 * 60 * 1000;   // hide after ~10 min paused
export const PENDING_TIMEOUT_MS = 8000;         // reconcile never came: revert

export function tickerState(enabled = true) {
  return { enabled: !!enabled, snap: null, pending: null, error: false };
}

export function setEnabled(s, on) {
  return { ...s, enabled: !!on };
}

/** now_playing snapshot arrived (event or command): the truth. Any real
 *  snapshot reconciles — it clears the pending optimistic flip (and the
 *  error latch), because the icon follows snap.playing from here on. */
export function applySnapshot(s, snap) {
  if (!snap || typeof snap !== 'object' || !snap.uri) return { ...s, snap: null, pending: null };
  return { ...s, snap, pending: null, error: false };
}

/** transport button pressed: {action, kind} with kind 'main' (play/pause)
 *  or 'next'. Optimistic: the main button flips right away. */
export function press(s, action, now = 0) {
  if (!s.snap || !s.snap.uri) return { state: s, send: null };
  const st = {
    ...s,
    error: false,
    pending: { action, wasPlaying: !!s.snap.playing, since: now },
  };
  if (action === 'toggle' && st.snap) {
    st.snap = { ...st.snap, playing: !st.snap.playing };
  }
  return { state: st, send: action };
}

/** the control POST answered: {ok, now_playing?}. Fresh truth reconciles;
 *  a failure reverts the optimistic flip and hides (error) until the next
 *  now_playing event proves things changed. */
export function resolveControl(s, resp) {
  if (resp && resp.ok) {
    const next = resp.now_playing ? applySnapshot({ ...s, pending: null }, resp.now_playing)
                                  : { ...s, pending: null };
    return next;
  }
  return revertPending(s);
}

export function revertPending(s) {
  if (!s.pending) return { ...s, error: true };
  const snap = s.snap
    ? { ...s.snap, playing: !!s.pending.wasPlaying }
    : s.snap;
  return { ...s, snap, pending: null, error: true };
}

/** decide what the bar shows. `overflow` is the painter's measurement
 *  (text wider than the bar); marquee applies only then. `now` is
 *  Date.now()-able ms. Returns null = hidden. */
export function tickerView(s, { overflow = false, now = 0 } = {}) {
  if (!s.enabled || s.error) return null;
  const snap = s.snap;
  if (!snap || !snap.uri || !snap.track) return null;
  // a stale optimistic flip whose truth never landed: revert, then decide
  if (s.pending && now - (s.pending.since || 0) > PENDING_TIMEOUT_MS) {
    return tickerView(revertPending(s), { overflow, now });
  }
  if (!snap.playing) {
    // paused: the bar stays (paused icon) for PAUSED_HIDE_MS counting from
    // the snapshot's arrival (`seenAt`, stamped by the painter)
    const pausedAt = Number(snap._seenAt) || now;
    if (now - pausedAt >= PAUSED_HIDE_MS) return null;
  }
  return {
    text: `${snap.track} — ${snap.artists || ''}`.replace(/ — $/, ''),
    playing: !!snap.playing,
    marquee: !!overflow,
    pending: !!s.pending,
  };
}
