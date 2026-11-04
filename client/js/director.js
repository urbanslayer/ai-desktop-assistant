// The animation director: a pure state machine that decides *what* she does,
// never *how* it moves. No imports except the gesture table (also pure), no
// three.js, no DOM — so it runs under `node --test client/js/`.
//
// Inputs  (all take an explicit `now` in seconds; the page passes
//          performance.now()/1000, tests pass whatever they like):
//   start(now)                    the page finished loading
//   status(now, state)            listening / hearing / thinking / speaking / idle
//   say(now, {emotion, seconds, text})  a spoken line (emotion/text may be absent)
//   heard(now, text)              the user's words arrived (text optional)
//   requestWave(now)              the W shortcut / riko-ctl wave: wave on demand
//   persona(now, info)              the persona (and body) changed; info = {id, avatar_version}
//   error(now)                    something went wrong
//   tick(now)                     every frame; runs timers (gesture end, cooldowns,
//                                 idle gestures, talk gestures, 10-min idle)
//
// Outputs: every method returns an array of actions for the page to apply,
// in order:
//   {type:'gesture', name, loop, duration}   play this gesture
//   {type:'gesture_stop', name}              stop this loop gesture
//   {type:'expression', name|null, value, hold, tilt, away}
//         blend the VRM expression to `value` (null = back to neutral);
//         `tilt` = lean the head, `away` = glance away (embarrassed)
//
// Rules honoured (docs/ROUNDS.md R1-A):
//   - a running one-shot gesture is never interrupted (loops can be swapped
//     or preempted by a high-priority gesture like the persona wave);
//   - a cooldown per gesture;
//   - wave on app start, on persona switch, when she comes back from
//     10+ minutes of doing nothing, on your greetings/goodbyes (heard or
//     typed text), when her reply starts with a greeting, and on demand
//     (W shortcut / riko-ctl wave); greeting waves honour the 25 s
//     cooldown, an ON-DEMAND wave always plays (it bypasses the cooldown
//     and blends out anything else running; only an already-playing wave
//     makes it skip);
//   - think loops while status is thinking, listen loops while listening;
//   - talk gestures sprinkled through long answers;
//   - stretch / look_around / fidget / hair_touch while idle every 1–3 min;
//   - emotions map to expressions + small gestures, exactly per ROUNDS.md.

import { GESTURES } from './gestures.js';

/** Emotion tag -> expression blend (per ROUNDS.md; unknown/absent -> neutral). */
export const EMOTION_EXPRESSIONS = {
  happy:       { expression: 'happy',     value: 1.0 },
  teasing:     { expression: 'happy',     value: 0.6, tilt: true },
  annoyed:     { expression: 'angry',     value: 0.5 },
  sad:         { expression: 'sad',       value: 0.8 },
  surprised:   { expression: 'surprised', value: 1.0 },
  thinking:    { expression: null,        value: 0 },
  embarrassed: { expression: 'happy',     value: 0.3, away: true },
  neutral:     { expression: null,        value: 0 },
};

/** Emotion tag -> cheap one-shot gesture (when one is free). */
export const EMOTION_GESTURES = {
  happy: 'nod', teasing: 'nod', sad: 'pout', annoyed: 'shake_head',
  surprised: null, embarrassed: 'pout', thinking: null, neutral: null,
};

const TALK_GESTURES = ['talk_1', 'talk_2', 'talk_3'];
const IDLE_GESTURES = ['stretch', 'look_around', 'fidget', 'hair_touch'];

// seconds a same gesture must wait before it may play again
export const COOLDOWNS = {
  wave: 25, stretch: 90, look_around: 90, fidget: 60, hair_touch: 90,
  nod: 8, shake_head: 8, laugh: 8, pout: 8,
  talk_1: 6, talk_2: 6, talk_3: 6,
};

export const RETURN_AFTER_IDLE = 600;  // 10 min of nothing -> she waves when you're back

// Greetings and goodbyes in your words earn a wave back (word boundaries, so
// "hi" never fires inside "this" and "later" never inside "calculator").
export const GREETING_RE = /\b(hi|hello|hey|hiya|good morning|good afternoon|good evening|good night|goodnight|goodbye|good bye|bye|see you|see ya|see ya later|later)\b/i;

// Her own reply waving back: only when it *opens* with a greeting (after any
// punctuation/emoji), not when a greeting merely appears mid-sentence.
export const REPLY_GREETING_RE = /^(?:["'“”\s*.,!~—-]|\p{Extended_Pictographic})*(?:hi|hello|hey|hiya|good (?:morning|afternoon|evening|night)|goodnight|goodbye|good bye|bye|see you|see ya)\b/iu;

// Pending one-shot priorities: higher wins the single pending slot, and >=5
// may also preempt a *loop* (never a one-shot).
const PRIO_EMOTION = 1, PRIO_IDLE = 2, PRIO_TALK = 3, PRIO_RETURN = 4, PRIO_PERSONA = 5;
const PRIO_GREET = PRIO_PERSONA;  // a greeting wave may swap a running loop

export class Director {
  constructor({ random = Math.random } = {}) {
    this.random = random;
    this.state = 'boot';
    this.gesture = null;        // {name, loop, endsAt}
    this.pending = null;        // {name, priority, expiresAt}
    this.expression = null;     // {name, until, tilt, away}
    this.cooldowns = {};        // name -> earliest next start (s)
    this.lastActivity = 0;
    this.returnWave = false;    // armed after 10 min idle, fires on your return
    this.nextIdleAt = Infinity;
    this.nextTalkAt = Infinity;
    this.lastTalk = null;
    this._personaKey = null;      // last persona id+version the page was told about
  }

  // ── inputs ──────────────────────────────────────────────────
  start(now) {
    this.lastActivity = now;
    this.state = 'idle';  // until the session says otherwise
    this.nextIdleAt = now + this._idleGap();
    return this.#play('wave', now, { priority: PRIO_RETURN });
  }

  /**
   * The persona (and body) changed. Pass the event's `{id, avatar_version}`
   * when you have it: repeated events for the SAME persona/version are
   * ignored, so a re-emitted hub event never re-waves or re-loads her.
   */
  persona(now, info = {}) {
    const key = info && info.id != null ? `${info.id}:${info.avatar_version}` : null;
    if (key && key === this._personaKey) return [];   // same persona, stale echo
    if (key) this._personaKey = key;
    this.lastActivity = now;
    this.returnWave = false;
    return this.#play('wave', now, { priority: PRIO_PERSONA, noCooldown: true });
  }

  heard(now, text = '') {
    const acts = this.#activity(now);
    if (text && GREETING_RE.test(String(text))) acts.push(...this.#greetingWave(now));
    return acts;
  }

  /**
   * The W shortcut / riko-ctl wave: an ON-DEMAND wave. It always plays right
   * away — the greeting cooldown never swallows it, and it blends out
   * whatever else is running (idle/talk/emotion one-shots and status loops
   * included). The only skip is a wave already playing, so mashing W never
   * restacks. (Social waves — greetings heard / greeting replies — keep the
   * calm #greetingWave rules.)
   */
  requestWave(now) {
    this.lastActivity = now;
    if (this.gesture?.name === 'wave') {
      // already waving: answer the press by dropping any queued wave echo,
      // so she waves once now, not twice in a row
      if (this.pending?.name === 'wave') this.pending = null;
      return [];
    }
    const acts = [];
    if (this.gesture) {  // blend out the running one-shot or loop
      acts.push({ type: 'gesture_stop', name: this.gesture.name });
      this.gesture = null;
    }
    this.pending = null;  // our wave beats anything queued behind it
    acts.push(...this.#play('wave', now, { priority: PRIO_GREET, noCooldown: true }));
    return this.#happyFace(acts, now);
  }

  /**
   * A social wave (greeting heard / greeting reply): respects the 25 s
   * cooldown, never stacks on a wave already running or queued, and carries
   * a happy face (the pose file adds the head tilt).
   */
  #greetingWave(now) {
    if (this.gesture?.name === 'wave' || this.pending?.name === 'wave') return [];
    return this.#happyFace(this.#play('wave', now, { priority: PRIO_GREET }), now);
  }

  /** The smile that rides along with any wave. */
  #happyFace(acts, now) {
    for (const a of acts) {
      if (a.type === 'gesture' && a.name === 'wave') {
        const hold = a.duration + 1.5;
        this.expression = { name: 'happy', until: now + hold, tilt: false, away: false };
        acts.push({ type: 'expression', name: 'happy', value: 0.8, hold, tilt: false, away: false });
      }
    }
    return acts;
  }

  /** A queued status loop only makes sense while its state still holds. */
  #prunePending() {
    const want = this.state === 'listening' || this.state === 'hearing' ? 'listen'
      : this.state === 'thinking' ? 'think' : null;
    if (this.pending && (this.pending.name === 'listen' || this.pending.name === 'think')
        && this.pending.name !== want) this.pending = null;
  }

  error(now) {
    this.lastActivity = now;
    return [];
  }

  status(now, state) {
    // repeated identical status (a state poll re-announcing the same state, a
    // hub replaying events after a restart) is a no-op: it must never restart
    // a running loop or re-arm timers
    if (!state || state === this.state) return [];
    this.state = state;
    this.#prunePending();
    const acts = [];
    if (state !== 'idle') acts.push(...this.#activity(now));

    const stopLoop = (name) => {
      if (this.gesture && this.gesture.name === name && this.gesture.loop) {
        this.gesture = null;
        acts.push({ type: 'gesture_stop', name });
      }
    };
    // start a status loop: replaces another loop right away; waits (pending)
    // behind a running one-shot; never restarts if already playing
    const startLoop = (name) => {
      if (this.gesture && this.gesture.name === name) return;
      if (this.gesture && this.gesture.loop) {
        const stopped = this.gesture.name;
        this.gesture = null;
        acts.push({ type: 'gesture_stop', name: stopped });
      }
      acts.push(...this.#play(name, now, { priority: PRIO_IDLE, noCooldown: true }));
    };
    switch (state) {
      case 'listening':
      case 'hearing':
        stopLoop('think');
        startLoop('listen');
        this.nextTalkAt = Infinity;
        break;
      case 'thinking':
        stopLoop('listen');
        startLoop('think');
        this.nextTalkAt = Infinity;
        break;
      case 'speaking':
        stopLoop('think');
        stopLoop('listen');
        this.nextTalkAt = now + 1.5 + this.random() * 2.5;
        break;
      case 'idle':
        stopLoop('think');
        stopLoop('listen');
        this.nextTalkAt = Infinity;
        if (!this.gesture) this.nextIdleAt = Math.min(this.nextIdleAt, now + this._idleGap());
        else this.pendingIdle = true;  // re-arm the gap once the body frees up
        break;
    }
    return acts;
  }

  say(now, { emotion = 'neutral', seconds = 0, text = '' } = {}) {
    const acts = [];
    this.lastActivity = now;
    // a reply that opens with a greeting waves along with it (cooldown applies)
    if (text && REPLY_GREETING_RE.test(String(text))) acts.push(...this.#greetingWave(now));
    const map = EMOTION_EXPRESSIONS[emotion] || EMOTION_EXPRESSIONS.neutral;
    this.expression = map.expression
      ? { name: map.expression, until: now + Math.max(seconds, 0) + 3,
          tilt: !!map.tilt, away: !!map.away }
      : null;
    acts.push({ type: 'expression', name: map.expression, value: map.value,
                hold: Math.max(seconds, 0) + 3, tilt: !!map.tilt, away: !!map.away });
    // a long happy line earns the bouncing laugh instead of the plain nod
    const g = emotion === 'happy' && seconds >= 6 ? 'laugh' : EMOTION_GESTURES[emotion];
    if (g) acts.push(...this.#play(g, now, { priority: PRIO_EMOTION }));
    if (emotion === 'thinking' && this.state !== 'thinking') {
      acts.push(...this.#play('think', now, { priority: PRIO_EMOTION, noCooldown: true }));
    }
    return acts;
  }

  tick(now) {
    const acts = [];
    // one-shot finished?
    if (this.gesture && !this.gesture.loop && now >= this.gesture.endsAt) {
      acts.push({ type: 'gesture_stop', name: this.gesture.name });
      this.gesture = null;  // anything queued behind it gets its turn (see below)
      if (this.pendingIdle) { this.pendingIdle = false; this.nextIdleAt = now + this._idleGap(); }
    }
    // a pending one-shot may go as soon as the body is free (or when it can
    // preempt a loop)
    this.#prunePending();
    if (this.pending) {
      const free = !this.gesture || (this.gesture.loop && this.pending.priority >= PRIO_PERSONA);
      if (now >= this.pending.expiresAt) this.pending = null;
      else if (free) {
        const p = this.pending; this.pending = null;
        acts.push(...this.#play(p.name, now, { priority: p.priority, noCooldown: p.noCooldown, allowPending: true }));
      }
    }
    // expression expiry: fade back to neutral
    if (this.expression && now >= this.expression.until) {
      this.expression = null;
      acts.push({ type: 'expression', name: null, value: 0, hold: 0, tilt: false, away: false });
    }
    // sprinkle talk gestures through a long answer
    if (this.state === 'speaking' && now >= this.nextTalkAt && !this.gesture) {
      const choices = TALK_GESTURES.filter((g) => g !== this.lastTalk && this.#ready(g, now));
      if (choices.length) {
        const pick = choices[Math.floor(this.random() * choices.length)];
        this.lastTalk = pick;
        acts.push(...this.#play(pick, now, { priority: PRIO_TALK }));
        this.nextTalkAt = now + GESTURES[pick].duration + 3 + this.random() * 4;
      } else {
        this.nextTalkAt = now + 2;  // all on cooldown; check again shortly
      }
    }
    // idle gestures
    if (this.state === 'idle' && !this.gesture && !this.returnWave && now >= this.nextIdleAt) {
      const choices = IDLE_GESTURES.filter((g) => this.#ready(g, now));
      if (choices.length) {
        acts.push(...this.#play(choices[Math.floor(this.random() * choices.length)],
                                now, { priority: PRIO_IDLE }));
      }
      this.nextIdleAt = now + this._idleGap();
    }
    // 10+ minutes of nothing: arm the welcome-back wave
    if (this.state === 'idle' && !this.returnWave && now - this.lastActivity >= RETURN_AFTER_IDLE) {
      this.returnWave = true;
    }
    return acts;
  }

  // ── internals ───────────────────────────────────────────────
  #activity(now) {
    const acts = [];
    this.lastActivity = now;
    if (this.returnWave) {
      this.returnWave = false;
      acts.push(...this.#play('wave', now, { priority: PRIO_RETURN, noCooldown: true }));
    }
    return acts;
  }

  #ready(name, now) {
    return (this.cooldowns[name] || 0) <= now;
  }

  #play(name, now, { priority = 1, noCooldown = false, allowPending = false } = {}) {
    const g = GESTURES[name];
    if (!g) return [];
    if (!noCooldown && !this.#ready(name, now)) return [];
    const busyOneShot = this.gesture && !this.gesture.loop;
    const busyLoop = this.gesture && this.gesture.loop;
    const blocked = busyOneShot
      || (busyLoop && !g.loop && priority < PRIO_PERSONA)  // one-shots don't interrupt loops...
      || (busyLoop && name === this.gesture.name);         // ...nor does the same loop restart
    if (blocked) {
      if (allowPending || (this.pending && this.pending.priority >= priority)) return [];
      this.pending = { name, priority, noCooldown, expiresAt: now + (g.loop ? 30 : 6) };
      return [];
    }
    // free slot, or we replace the running loop (status loops and priority >= 5)
    this.gesture = { name, loop: g.loop, endsAt: now + g.duration };
    this.cooldowns[name] = now + (COOLDOWNS[name] || 0);
    return [{ type: 'gesture', name, loop: g.loop, duration: g.duration }];
  }

  _idleGap() {
    return 60 + this.random() * 120;  // 1–3 min per ROUNDS.md
  }
}
