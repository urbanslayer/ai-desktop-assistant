"""One conversation, driven from anywhere: the terminal, the desktop app, or riko-ctl.

Turns (typed text, a recorded clip, or a persona switch) go into a queue and
a single worker thread handles them in order:

    clip -> asr.transcribe -> text -> hub.chat -> tts (this PC, or the
    hub's audio_url with tts: hub) -> viewer.play

Every step is reported to the avatar page as a UI event, so the desktop
window can show "listening...", "thinking...", her speech bubble, and swap
the VRM when the persona changes — by keyword, riko-ctl or another client.
"""
import collections
import json
import queue
import threading
import time
from typing import Callable, Optional

from riko import appstate, asr, client_tools, hub_client, name_gate, tts, viewer

ECHO_TAIL_MS_DEFAULT = 200  # VAD stays paused this long after her voice ends
                            # (echo guard); override: desktop.always_listen.echo_tail_ms
LISTEN_MODES = ("push_to_talk", "always")
MUSIC_RECONCILE_S = 5.0     # fast-path hub-confirm dedupe window (ticker)


class Session:
    def __init__(self, cfg, source="desktop"):
        self.cfg = cfg
        self.source = source  # stamped on every message the hub stores
        self.hub = hub_client.get()
        hub_client.note_active(self.hub.state())  # who is active right now?
        # riko/music/mpris.py subscribes here to receive every reconciled
        # now_playing snapshot (same dict as the music:state command);
        # desktop.py wires it. None = no MPRIS export (docs/SOLOIST.md).
        self.on_music_state: Optional[Callable[[dict], None]] = None
        self.recorder = asr.Recorder()
        self._turns = queue.Queue()
        self._hub_audio_down = False  # hub voice trouble: print once, not per turn
        self._start_greet_done = False  # the app-start hello happens exactly once
        # ── listening mode (push_to_talk | always) ───────────────────
        # The runtime choice is persisted (~/.config/riko/listen_mode.json);
        # desktop.listen_mode in the yaml only seeds the initial default.
        cfg_mode = str((cfg.get("desktop") or {}).get("listen_mode") or "push_to_talk")
        mode = appstate.load_mode() or cfg_mode
        self.listen_mode = mode if mode in LISTEN_MODES else "push_to_talk"
        self.on_listen_mode: Optional[Callable[[str], None]] = None  # desktop.py: refreshes the tray
        # ── name gate for always mode (riko/name_gate.py) ───────────────
        # On, VAD utterances only pass when they address the active persona
        # (hub name + keywords + listen.name_aliases) — or another persona,
        # which routes through the hub switch. Push-to-talk and typed text
        # are intent and never gated. The choice is persisted next to the
        # mode (~/.config/riko/listen_name_gate.json); $RIKO_NAME_GATE and
        # listen.name_gate in the yaml seed it.
        self.name_gate = name_gate.resolve_initial_gate(cfg)
        self.on_name_gate: Optional[Callable[[bool], None]] = None  # desktop.py: tray tick
        self._gate_window = name_gate.FollowWindow(name_gate.follow_up_seconds(cfg))
        self._gate_any = name_gate.any_persona_enabled(cfg)
        self._gate_cache = None  # (fresh_until, active_id, entries): personas + aliases
        self._gate_seen_active = None  # last active persona id seen at a gate check
        self._vad = None             # asr.AlwaysListener while always mode runs
        self._vad_holds = 0          # >0 = VAD paused (thinking/speaking/Space held)
        self._vad_lock = threading.Lock()
        self._spoke_last_turn = False  # worker-only: did the last turn make sound?
        # Music phase 1 (docs/SPOTIFY_NOTES.md): desktop.py wires a ducking
        # hook around speech — fn(True) when she starts talking, fn(False)
        # when she stops. None = no ducking (hub-only runs).
        self.on_speak: Optional[Callable[[bool], None]] = None
        # ── hub live events (docs/TIMERS.md D) ──────────────────────────
        self.on_timer_notice: Optional[Callable[[str, str], None]] = None  # desktop.py: tray
        # Dictation router (docs/TYPING.md §8), wired by desktop.py:
        # fn(text) -> (handled, replacement). handled=True consumes the
        # utterance client-side (typed into the pinned window / control
        # phrase): no hub turn, no reply. Otherwise `replacement` (when a
        # str) replaces the text for the normal chat turn (vocative strip).
        self.dictation_filter: Optional[Callable[[str], tuple]] = None
        self._events_stop = None     # stop Event of the events follower thread
        # song ticker fast path (docs/SPOTIFY_NOTES.md): (uri, playing,
        # wall-clock) of the last snapshot forwarded from the desktop's
        # local Soloist listener; hub events matching it within the
        # reconcile window are confirmations, not new states (no flicker).
        self._music_fast = None
        self._timer_lock = threading.Lock()
        self._timer_played = collections.deque(maxlen=512)   # message_ids already queued to speak
        self._timer_noticed = collections.deque(maxlen=512)  # message_ids already sent to the tray
        self._action_lock = threading.Lock()
        self._action_played = collections.deque(maxlen=512)  # 'action' event seqs already forwarded
        if self.listen_mode == "always" and self.source == "desktop":
            if not self._vad_start():
                self.listen_mode = "push_to_talk"
            else:
                print("[listen] always on")
        threading.Thread(target=self._worker, daemon=True, name="riko-session").start()

    # ── Listening mode ───────────────────────────────────────
    def _vad_cfg(self):
        return dict((self.cfg.get("desktop") or {}).get("always_listen") or {})

    def _echo_tail_seconds(self):
        """Echo guard after her voice ends, from desktop.always_listen.echo_tail_ms."""
        try:
            ms = float(self._vad_cfg().get("echo_tail_ms", ECHO_TAIL_MS_DEFAULT))
        except (TypeError, ValueError):
            ms = float(ECHO_TAIL_MS_DEFAULT)
        return min(max(ms, 0.0), 2000.0) / 1000.0

    def _vad_start(self):
        """Open the continuous mic stream. False = broken mic (caller falls back)."""
        if self._vad is None:
            try:
                # VAD-originated clips carry the Silero stats the noise
                # filter weighs (no pill, evidence filter — see _turn).
                self._vad = asr.AlwaysListener(
                    lambda wav, stats=None: self.submit_audio(
                        wav, source="vad", vad_stats=stats),
                    cfg=self._vad_cfg())
            except Exception as e:  # even constructing can fail (no numpy/sounddevice)
                print(f"[listen] always-listen can't start: {e} — staying with push-to-talk")
                return False
            with self._vad_lock:
                self._vad_holds = 0  # a fresh listener is never paused (a toggle
                # mid-turn may have left a stale count from the closed one)
        try:
            self._vad.start()
        except Exception as e:
            print(f"[listen] always-listen can't open the mic: {e} — "
                  "staying with push-to-talk")
            self._vad = None
            return False
        return True

    def set_listen_mode(self, mode, persist=True):
        """Switch ears: hold Space (with the listening pill) or hands-free VAD."""
        if mode not in LISTEN_MODES:
            raise ValueError(f"unknown listen mode {mode!r} "
                             f"(use {LISTEN_MODES[0]} or {LISTEN_MODES[1]})")
        if mode == self.listen_mode:
            return
        if mode == "always":
            if not self._vad_start():
                # dead mic: printed once, stay with push-to-talk, don't persist
                if self.on_listen_mode:
                    self.on_listen_mode(self.listen_mode)
                return
            self.listen_mode = mode
            print("[listen] always on")
        else:
            vad_ref = self._vad
            self._vad = None
            self.listen_mode = mode
            print("[listen] push-to-talk")
            if vad_ref is not None:
                # Never close the mic on the caller: this is often the Qt
                # thread (L key / tray) or the riko-ctl HTTP thread, and the
                # reader thread must dismantle its own PortAudio stream
                # without a foreign close racing it. Hand the shutdown to a
                # short-lived thread and update the tray/UI right away.
                threading.Thread(target=vad_ref.close, daemon=True,
                                 name="riko-vad-close").start()
        if persist:
            appstate.save_mode(mode)  # remembered for the next start
        if self.on_listen_mode:
            self.on_listen_mode(self.listen_mode)

    def toggle_listen_mode(self):
        self.set_listen_mode("always" if self.listen_mode == "push_to_talk"
                             else "push_to_talk")

    # ── Name gate (always mode only) ─────────────────────────
    def set_name_gate(self, on, persist=True):
        """The tray's 'Only when I say her name' / riko-ctl name-gate. The
        gate only bites in always mode: push-to-talk is already intent."""
        on = bool(on)
        if on != self.name_gate:
            self.name_gate = on
            print(f"[listen] name gate {'on — only when you say her name' if on else 'off'}")
            if persist:
                appstate.save_name_gate(on)
        if self.on_name_gate:  # keep the tray honest even for no-op sets
            self.on_name_gate(self.name_gate)

    def _gate_entries(self):
        """The personas that can be addressed: hub name + keywords + the
        configured aliases, active one flagged. Refreshed on persona
        switches and whenever this cache goes stale (name_gate TTL)."""
        active = hub_client.active_persona() or {}
        active_id = active.get("id")
        now = time.monotonic()
        cache = self._gate_cache
        if cache and cache[1] == active_id and now < cache[0]:
            return cache[2]
        try:
            listed = self.hub.personas()
        except Exception:  # hub hiccup: the active persona can still answer
            listed = None
        if not listed:
            listed = [active] if active else []
        entries = name_gate.build_entries(self.cfg, listed, active_id)
        if active_id and not any(e["id"] == active_id for e in entries):
            entries.insert(0, {"id": active_id,
                               "name": active.get("name") or active_id,
                               "names": name_gate.persona_names(
                                   active, name_gate.alias_map(self.cfg).get(active_id)),
                               "active": True})
        self._gate_cache = (now + name_gate.PERSONA_CACHE_TTL, active_id, entries)
        return entries

    def _gate_refresh(self):
        self._gate_cache = None

    def _gate_switch(self, entry):
        """Addressed a DIFFERENT persona by name: switch through the hub's
        own path (POST /api/active — the same call the tray menu makes), then
        the utterance goes on to the newly active persona."""
        print(f"[asr] addressed {entry['name']}: switching")
        self._gate_window.close()  # a persona switch always resets the window
        try:
            state = self.hub.set_active(entry["id"])
        except Exception as e:
            print(f"[listen] switch to {entry['name']} failed: {e}")
            return
        hub_client.announce_persona(state)
        self._gate_refresh()

    def _gate_accept(self, text) -> bool:
        """Worker-thread gate decision for one VAD utterance (already
        corrected). True = send it; the follow-up window restarts."""
        active_id = (hub_client.active_persona() or {}).get("id")
        if (self._gate_seen_active is not None and active_id
                and active_id != self._gate_seen_active):
            # a switch made elsewhere (web page, another PC, the poller):
            # new persona, new name to say — the window is over
            self._gate_window.close()
        self._gate_seen_active = active_id
        active_hit, other = name_gate.find_mention(text, self._gate_entries())
        if other is not None and self._gate_any:
            self._gate_switch(other)
        elif active_hit is None and not self._gate_window.open():
            print(f"[asr] ignored (no name): {text[:40]}")
            return False
        self._gate_window.restart()
        return True

    def _vad_hold(self, on):
        """Pause (drop frames, reset state) or resume the VAD."""
        if self._vad is None:
            return
        with self._vad_lock:
            self._vad_holds += 1 if on else -1
            if self._vad_holds > 0:
                self._vad.pause()
            elif self._vad_holds == 0:
                self._vad.resume()

    def close(self):
        """Release the mic and the events follower on shutdown."""
        if self._vad is not None:
            self._vad.close()
            self._vad = None
        if self._events_stop is not None:
            self._events_stop.set()

    # ── Ways in ──────────────────────────────────────────────
    def greet_on_start(self, page_timeout=120.0):
        """Say hello once when the app starts (desktop.py / main.py call this
        at startup; desktop.greet_on_start in character_config.yaml, on by
        default).

        Runs on its own thread, so startup never blocks: it waits for the
        avatar page to be ready — its first /next poll lands when the VRM has
        loaded and director.start() has begun the startup wave — then queues
        the SAME greeting turn a persona switch speaks: the active persona's
        greeting in a happy bubble, in her voice (the hub's greeting audio
        with tts: hub, this PC's TTS otherwise, bubble only with tts: none).
        Because it lands seconds after wake, it rides the startup wave instead
        of adding a second one, and it never touches the hub's chat history.
        A second call does nothing (reconnects and polls must not re-greet).
        """
        if self._start_greet_done:
            return
        self._start_greet_done = True
        if not (self.cfg.get("desktop") or {}).get("greet_on_start", True):
            return
        active = hub_client.active_persona()
        if not active:
            print("[greet] the hub has no active persona; skipping the hello")
            return

        def wait_and_greet():
            deadline = time.monotonic() + page_timeout
            while not viewer.page_open() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not viewer.page_open():
                print("[greet] the avatar page never loaded; skipping the hello")
                return
            self._turns.put(("greet", dict(active)))

        threading.Thread(target=wait_and_greet, daemon=True, name="riko-greet").start()

    def submit_text(self, text):
        text = str(text).strip()
        if not text:
            return
        viewer.emit("heard", text=text)
        self._turns.put(("text", text))

    def submit_audio(self, wav_bytes, source="ptt", vad_stats=None):
        """Queue a recorded clip. source='ptt' = a deliberate recording (Space,
        Enter in the terminal); source='vad' = an always-mode segment, with
        vad_stats = the Silero numbers the segmenter measured (seconds,
        voiced_seconds, mean_prob). The turn behaves differently: VAD segments
        show no 'hearing' pill and are judged on Whisper+Silero evidence
        (real short 'Thank you.'s pass); PTT clips show it and only drop on
        an empty transcript."""
        self._turns.put(("audio", (wav_bytes, source, vad_stats)))

    def switch_persona(self, name):
        """Switch by id, name or keyword — raises HubError (404) if nothing matches.
        The page reloads the avatar right away; the greeting is spoken on the worker."""
        state = self.hub.set_active(name)
        hub_client.announce_persona(state)
        active = state["active"]
        print(f"[persona] {active['name']} is in now")
        # New persona, new name to say: the follow-up window closes and the
        # gate's persona snapshot is rebuilt on the next utterance.
        self._gate_window.close()
        self._gate_refresh()
        self._turns.put(("greet", active))
        return active

    def persona_next(self):
        """Cycle to the next persona on the hub (wraps around to the first).
        Announced exactly like any other switch; returns the new active persona."""
        listed = self.hub.personas()
        if not listed:
            raise hub_client.HubError("persona-next: the hub has no personas")
        ids = [p["id"] for p in listed]
        active_id = (hub_client.active_persona() or {}).get("id")
        nxt = listed[(ids.index(active_id) + 1) % len(listed)] if active_id in ids else listed[0]
        return self.switch_persona(nxt["id"])

    # ── Hub live events (docs/TIMERS.md D) ───────────────────
    def follow_hub_events(self, wait=25.0):
        """Follow the hub's events long-poll so turns the HUB starts — fired
        timers — reach her like any other reply (docs/TIMERS.md D). The
        desktop app starts this in hub mode; idempotent. Timer replies queue
        into the worker, so they wait behind whatever she is doing now and
        ride the same VAD hold / echo tail as a normal turn."""
        if self._events_stop is None:
            self._events_stop = hub_client.start_events_follower(
                self._on_hub_event, client=self.hub, wait=wait)
            threading.Thread(target=self._seed_now_playing,
                             daemon=True).start()

    def _seed_now_playing(self):
        """Late-client seed for the song ticker (docs/SPOTIFY_NOTES.md): the
        page opens mid-song, so ask the hub once what is playing. Best
        effort: 409 (not connected) or any hub hiccup means no ticker."""
        try:
            snap = hub_client.get().spotify_now_playing()
        except Exception:
            return
        if isinstance(snap, dict) and (snap.get("uri") or snap.get("track")):
            self._emit_music_state(snap)

    def _on_now_playing(self, ev):
        """now_playing event (follower thread) -> page ticker command.
        Forwarded even when empty: the idle snapshot (uri '') is exactly
        what tells the page to hide the ticker now. A hub event that
        CONFIRMS the fast path's last local Soloist snapshot (same uri +
        playing, inside MUSIC_RECONCILE_S) is skipped — the page already
        painted that state; re-sending it would double-paint, and the
        hub is by definition the slower witness of the same change."""
        fast = self._music_fast
        if fast is not None and \
                time.time() - fast[2] < MUSIC_RECONCILE_S and \
                str(ev.get("uri") or "") == fast[0] and \
                bool(ev.get("playing")) == fast[1]:
            return
        self._emit_music_state(ev)

    def music_local(self, snap):
        """FAST PATH (docs/SPOTIFY_NOTES.md): the desktop's Soloist
        listener saw a local playback change. Forward it to the page at
        once (sub-second ticker, zero Spotify calls) and nudge the hub's
        fast poll so its authoritative now_playing event confirms or
        replaces this snapshot — newer state always wins. Called from the
        listener thread (never Qt); the nudge is best effort."""
        self._music_fast = (str(snap.get("uri") or ""),
                            bool(snap.get("playing")), time.time())
        self._emit_music_state(snap)
        try:
            self.hub.spotify_wake()
        except Exception:
            pass  # old hub / offline: the idle watch still notices

    # keys the ticker understands; the event envelope keys stay behind
    _MUSIC_KEYS = ("playing", "uri", "track", "artists", "album",
                   "duration_ms", "position_ms", "device", "context_uri")

    def _emit_music_state(self, snap):
        payload = {k: snap.get(k) for k in self._MUSIC_KEYS if k in snap}
        viewer.emit("command", cmd="music:state " + json.dumps(
            payload, ensure_ascii=False))
        # MPRIS export (riko/music/mpris.py): the SAME reconciled state goes
        # to the session-bus player. Best-effort like viewer.emit — a broken
        # hook must never break the ticker path.
        cb = getattr(self, "on_music_state", None)
        if cb is not None:
            try:
                cb(dict(snap))
            except Exception as e:
                print(f"[music] state hook failed: {e}", flush=True)

    def _on_action_preview(self, ev):
        """'action' event = the web Animations tab's Preview button
        (hub/server.py preview route): forward its actions to the avatar
        page as an 'actions' page event. The page registers the actions'
        inline `animation` copies BEFORE the director validates the names
        (docs/SELF_ANIMATION.md, the live curtsey_twirl bug), then schedules
        them through director.say exactly like a reply's actions — only
        without the bubble or the voice. viewer.emit is a queue put, so this
        stays cheap on the follower thread (never Qt)."""
        if str(ev.get("persona_id") or ev.get("persona") or "") != self._active_id():
            return  # another persona's preview: her avatar is not the one on screen
        actions = ev.get("actions")
        if not isinstance(actions, list) or not actions:
            return
        # Only Preview-button actions. /api/touch ALSO publishes 'action'
        # (with a `touch` payload) but its reply already carried those
        # actions to this desktop (play_touch_reply) — forwarding them again
        # would play every touch gesture twice.
        if ev.get("touch") or not any(
                isinstance(a, dict) and a.get("preview") for a in actions):
            return
        key = (ev.get("seq"), ev.get("time"))
        with self._action_lock:
            if key in self._action_played:  # ring replay / double delivery
                return
            self._action_played.append(key)
        viewer.emit("actions", actions=actions)

    def _on_hub_event(self, kind, ev):
        """One hub event (follower thread). Only timer traffic matters here:
        every other turn was already played by the call that started it."""
        if kind == "timer_fired":
            self._timer_notice(ev)
            return
        if kind == "animations_changed":
            self._on_animations_changed(ev)  # custom animations list refreshed
            return
        if kind == "now_playing":
            self._on_now_playing(ev)  # song ticker (docs/SPOTIFY_NOTES.md)
            return
        if kind == "action":
            self._on_action_preview(ev)  # web Animations tab Preview (SELF_ANIMATION.md)
            return
        if kind != "turn_finished" or ev.get("origin") != "timer":
            return  # a normal turn (no origin) was spoken by its own /api/chat
        if str(ev.get("persona_id") or ev.get("persona") or "") != self._active_id():
            return  # another persona's timer: the tray line came from timer_fired
        message_id = ev.get("message_id")
        with self._timer_lock:
            if message_id in self._timer_played:  # ring replay / double delivery
                return
            self._timer_played.append(message_id)
        self._turns.put(("timer", dict(ev)))

    def _timer_notice(self, ev):
        """A timer fired for a NON-active persona: one tray line, no switch
        ('⏰ <persona>: <label>'). Active-persona timers are spoken, not shown."""
        persona_id = str(ev.get("persona_id") or "")
        if not persona_id or persona_id == self._active_id():
            return
        message_id = ev.get("message_id")
        with self._timer_lock:
            if message_id in self._timer_noticed:
                return
            self._timer_noticed.append(message_id)
        label = str(ev.get("label") or "").strip()
        if self.on_timer_notice:
            try:
                self.on_timer_notice(self._persona_name(persona_id), label)
            except Exception as e:
                print(f"[timer] notice failed: {type(e).__name__}: {e}")

    def _on_animations_changed(self, ev):
        active = hub_client.active_persona() or {}
        pid = str(ev.get("persona_id") or ev.get("persona") or "")
        if pid and pid != str(active.get("id") or ""):
            return  # another persona's upload: the active list did not change
        hub_client.refresh_custom_animations()  # follower thread, never Qt

    def _persona_name(self, persona_id):
        active = hub_client.active_persona() or {}
        if active.get("id") == persona_id:
            return active.get("name") or persona_id
        try:
            for p in self.hub.personas():
                if p.get("id") == persona_id:
                    return p.get("name") or persona_id
        except Exception:  # hub hiccup: the id still identifies her
            pass
        return persona_id

    def talk_start(self):
        if self.recorder.recording:
            return
        if self._vad is not None:
            self._vad.suspend()  # Space held in always mode: the recorder takes the mic
        try:
            self.recorder.start()
        except Exception as e:  # no mic, device busy...
            print(f"[asr] can't open the mic: {e}")
            viewer.emit("error", text=f"Mic error: {e}")
            if self._vad is not None:
                self._vad.release()
            return
        print("[asr] listening...")
        viewer.emit("status", state="listening")

    def talk_stop(self):
        if not self.recorder.recording:
            return
        try:
            clip = self.recorder.stop()
        except Exception as e:  # a mic hiccup must not kill the caller (usually the HTTP handler)
            print(f"[asr] can't stop the mic: {e}")
            viewer.emit("error", text=f"Mic error: {e}")
            viewer.emit("status", state="idle")
            if self._vad is not None:
                self._vad.release()
            return
        if self._vad is not None:
            self._vad.release()  # always mode: the VAD takes the mic back
        if clip is None:
            viewer.emit("status", state="idle")
            return
        self.submit_audio(clip, source="ptt")

    def talk_toggle(self):
        (self.talk_stop if self.recorder.recording else self.talk_start)()

    def forget(self):
        self.hub.clear_history()  # the active persona's chat, on the hub
        viewer.emit("say", text="...Who are you again, senpai?")
        print("[hub] chat history cleared")

    # ── The worker ───────────────────────────────────────────
    def _worker(self):
        while True:
            kind, payload = self._turns.get()
            # She must not hear herself: the VAD ignores frames and forgets
            # any half-built segment for the whole turn — thinking AND
            # speaking (viewer.play returns when the page says the clip
            # ended) — plus a short echo tail after her voice stops.
            self._vad_hold(True)
            self._spoke_last_turn = False
            try:
                self._turn(kind, payload)
            except Exception as e:  # one bad turn should not end the chat
                print(f"[error] {type(e).__name__}: {e}")
                viewer.emit("error", text=f"{type(e).__name__}: {e}")
            finally:
                # The ears come back the moment the echo settles — and only
                # for the echo: a turn that made no sound (ignored noise,
                # tts: none) resumes instantly. Everything else (idle status,
                # bookkeeping) waits behind the resume: every ms here is time
                # she cannot hear you.
                if self._vad is not None:
                    if self._spoke_last_turn:
                        time.sleep(self._echo_tail_seconds())
                    self._vad_hold(False)
                if self._turns.empty() and not self.recorder.recording:
                    viewer.emit("status", state="idle")

    def _turn(self, kind, payload):
        if kind == "greet":
            viewer.emit("status", state="thinking")
            greeting = payload.get("greeting") or f"{payload['name']} here."
            # switch greetings earn a wave when this body can wave (docs/BODY.md §3)
            greet_actions = ([{"name": "wave", "at": 0}]
                             if "wave" in client_tools.current_body_actions() else [])
            self._speak(greeting, audio_url=self._greeting_audio_url(payload),
                        emotion="happy", actions=greet_actions)
            return
        if kind == "timer":
            # docs/TIMERS.md D: a fired timer's reply, started by the HUB. It
            # plays exactly like a normal reply — bubble, hub voice from
            # audio_url, emotion, actions — and because it lands on this
            # worker it waits behind whatever she is doing now and rides the
            # same VAD hold / echo tail as any other turn.
            reply = str(payload.get("reply") or "").strip()
            if not reply:
                return
            self._speak(reply, audio_url=payload.get("audio_url"),
                        emotion=payload.get("emotion") or "neutral",
                        actions=payload.get("actions") or [],
                        speech=payload.get("speech"))
            return
        if kind == "audio":
            wav, source, vad_stats = payload
            if source == "ptt":
                # deliberate recording: the pill is honest feedback. VAD
                # segments show nothing until the evidence filter has
                # spoken — TV noise must not flash 'hmm?'.
                viewer.emit("status", state="hearing")
                text = asr.transcribe(wav, self.cfg)
                reason = asr.hallucination_reason(text)
            else:
                # VAD segment: ask for Whisper's own confidence metrics too;
                # the clip is only dropped when the evidence says nobody
                # spoke — a real "Thank you." passes.
                text, evidence = asr.transcribe_verbose(wav, self.cfg)
                reason = asr.vad_noise_reason(text, evidence, vad_stats)
            if reason:  # nobody actually spoke: drop the turn
                print(f"[asr] ignored: {text.strip()!r} ({reason})")
                if not text.strip() and source == "ptt":
                    print("(heard nothing, try again)")
                    viewer.emit("error", text="(heard nothing, try again)")
                return
            # fix words Whisper misheard (Farina -> Furina): the corrected
            # text is what the bubble shows and what the hub ever sees
            text = asr.apply_corrections(text, self.cfg).strip()
            # Dictation runs FIRST (docs/TYPING.md §8): it types everything
            # into the pinned window and must never see the name gate. The
            # 'heard' bubble lands after the router so a dictated line shows
            # in the order the owner experiences it.
            if self.dictation_filter is not None:
                try:
                    handled, replacement = self.dictation_filter(text)
                except Exception as e:  # noqa: BLE001 — never eat her turn
                    print(f"[dictation] router failed: {type(e).__name__}: {e}")
                else:
                    if handled:
                        viewer.emit("heard", text=text)
                        viewer.emit("status", state="idle")
                        return
                    if isinstance(replacement, str):
                        text = replacement.strip()
                        if not text:
                            viewer.emit("status", state="idle")
                            return
            # The name gate (after the corrections and the dictation router):
            # VAD segments only; push-to-talk and typed text are intent.
            # An ignored utterance never reaches the hub, the bubble or the
            # pill — one terminal line is all the owner sees.
            if (source == "vad" and self.name_gate
                    and self.listen_mode == "always"
                    and not self._gate_accept(text)):
                return
            viewer.emit("heard", text=text)
        else:
            text = payload
        print(f"You:  {text}")
        viewer.emit("status", state="thinking")
        # voice=True (docs/API.md): this text came off a mic — the hub tells
        # her never to comment on misheard names, and marks the 🎤 flag
        result = self.hub.chat(text, source=self.source, voice=kind == "audio")
        # 'switch to asuka' and bare keywords come back as switched, with the
        # greeting as the reply; a persona change also reloads the avatar page.
        switched = bool(result.get("switched")) or result.get("persona") != self._active_id()
        if switched:
            hub_client.announce_persona(self.hub.state())
        # tts: hub — the hub already made (or will make) the audio; audio_url
        # is null when TTS is off or this persona's voice is, so: bubble only.
        # emotion may be absent from older hubs: the viewer treats that as neutral.
        # actions (docs/BODY.md §3) may be absent from older hubs too: [] then.
        self._speak(result["reply"], audio_url=result.get("audio_url"),
                    emotion=result.get("emotion") or "neutral",
                    actions=result.get("actions") or [],
                    speech=result.get("speech"))
        # Follow-up window: she just answered, so for a moment she keeps
        # hearing you without the name (0 s disables it). A mid-turn persona
        # switch closes the window instead — new persona, new name to say.
        if switched:
            self._gate_window.close()
            self._gate_refresh()
        else:
            self._gate_window.restart()

    def _speak(self, answer, audio_url=None, emotion="neutral", actions=None,
               speech=None):
        """Bubble + voice for one reply. The bubble always shows the full
        `answer`; the voice gets the hub's cleaned `speech` (ChatResult.speech)
        or, on an older hub, our own cleaned copy (riko/tts.py → riko/speech.py)."""
        persona = hub_client.active_persona() or {}
        print(f"{persona.get('name', 'Riko')}: {answer}")
        if self.cfg["tts"] == "hub":
            wav = self._hub_audio(audio_url)
        else:
            wav = tts.speak(answer, self.cfg, voice=persona.get("tts_voice") or None,
                            speech=speech)
        viewer.emit("status", state="speaking")
        self._spoke_last_turn = wav is not None
        # duck the desktop music player while she talks (best-effort; the
        # hook itself never raises, but a dumb desktop hook shouldn't kill
        # the reply either)
        if self.on_speak is not None:
            try:
                self.on_speak(True)
            except Exception:
                pass
        viewer.play(wav, answer, emotion=emotion, actions=actions or [])
        if self.on_speak is not None:
            try:
                self.on_speak(False)
            except Exception:
                pass
        if self._vad is not None and wav is not None:
            # Her voice is really over NOW (the page reported it): audio from
            # here on is echo-era and may pre-roll the next VAD segment.
            self._vad.tail()

    def _hub_audio(self, audio_url):
        """Fetch reply/greeting audio the hub made. None = speech bubble only;
        a hub voice failure is printed once (not every turn) and never fatal."""
        if not audio_url:
            return None
        try:
            wav = tts.from_hub_audio(self.hub.get_audio(audio_url))
            self._hub_audio_down = False  # back to normal, next failure gets one line again
            return wav
        except Exception as e:  # hub 502/503/409, torn-down connection, bad bytes...
            if not self._hub_audio_down:
                print(f"[tts] hub audio failed ({type(e).__name__}: {e}); "
                      "her reply shows in the bubble only")
                self._hub_audio_down = True
            return None

    def play_touch(self, reply):
        """Play a POST /api/touch reaction exactly like a chat reply:
        bubble + TTS + actions (docs/TOUCH.md). Runs on the touch-forward
        worker thread — viewer.play waits for the clip. reacted=false
        (off / cooldown / limit / busy / silent kind) returns at once."""
        if not reply or not (reply.get("reacted") or reply.get("spoke")):
            return
        text = str(reply.get("text") or "").strip()
        if not text:
            return
        try:
            self._speak(text, audio_url=reply.get("audio_url"),
                        emotion=reply.get("emotion") or "neutral",
                        actions=reply.get("actions") or [])
        except Exception as e:  # a torn-down hub must not kill the touch path
            print(f"[touch] could not play her reaction: {type(e).__name__}: {e}")

    def _greeting_audio_url(self, persona):
        """Where the hub keeps a persona's greeting audio (only with tts: hub);
        None on an old hub without voice fields or when her voice is off."""
        if self.cfg["tts"] != "hub":
            return None
        if ((persona or {}).get("voice") or {}).get("mode") == "off":
            return None
        url = f"/api/personas/{persona['id']}/greeting/audio"
        version = (persona or {}).get("voice_version")
        return f"{url}?v={version}" if version is not None else url

    def _active_id(self):
        active = hub_client.active_persona() or {}
        return active.get("id")
