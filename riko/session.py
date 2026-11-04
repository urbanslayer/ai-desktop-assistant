"""One conversation, driven from anywhere: the terminal, the desktop app, or riko-ctl.

Turns (typed text, a recorded clip, or a persona switch) go into a queue and
a single worker thread handles them in order:

    clip -> asr.transcribe -> text -> hub.chat -> tts (this PC, or the
    hub's audio_url with tts: hub) -> viewer.play

Every step is reported to the avatar page as a UI event, so the desktop
window can show "listening...", "thinking...", her speech bubble, and swap
the VRM when the persona changes — by keyword, riko-ctl or another client.
"""
import queue
import threading
import time
from typing import Callable, Optional

from riko import appstate, asr, client_tools, hub_client, tts, viewer

ECHO_TAIL_MS_DEFAULT = 200  # VAD stays paused this long after her voice ends
                            # (echo guard); override: desktop.always_listen.echo_tail_ms
LISTEN_MODES = ("push_to_talk", "always")


class Session:
    def __init__(self, cfg, source="desktop"):
        self.cfg = cfg
        self.source = source  # stamped on every message the hub stores
        self.hub = hub_client.get()
        hub_client.note_active(self.hub.state())  # who is active right now?
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
        self._vad = None             # asr.AlwaysListener while always mode runs
        self._vad_holds = 0          # >0 = VAD paused (thinking/speaking/Space held)
        self._vad_lock = threading.Lock()
        self._spoke_last_turn = False  # worker-only: did the last turn make sound?
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
            if self._vad is not None:
                self._vad.close()
                self._vad = None
            self.listen_mode = mode
            print("[listen] push-to-talk")
        if persist:
            appstate.save_mode(mode)  # remembered for the next start
        if self.on_listen_mode:
            self.on_listen_mode(self.listen_mode)

    def toggle_listen_mode(self):
        self.set_listen_mode("always" if self.listen_mode == "push_to_talk"
                             else "push_to_talk")

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
        """Release the mic on shutdown."""
        if self._vad is not None:
            self._vad.close()
            self._vad = None

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
            viewer.emit("heard", text=text)
        else:
            text = payload
        print(f"You:  {text}")
        viewer.emit("status", state="thinking")
        result = self.hub.chat(text, source=self.source)
        # 'switch to asuka' and bare keywords come back as switched, with the
        # greeting as the reply; a persona change also reloads the avatar page.
        if result.get("switched") or result.get("persona") != self._active_id():
            hub_client.announce_persona(self.hub.state())
        # tts: hub — the hub already made (or will make) the audio; audio_url
        # is null when TTS is off or this persona's voice is, so: bubble only.
        # emotion may be absent from older hubs: the viewer treats that as neutral.
        # actions (docs/BODY.md §3) may be absent from older hubs too: [] then.
        self._speak(result["reply"], audio_url=result.get("audio_url"),
                    emotion=result.get("emotion") or "neutral",
                    actions=result.get("actions") or [],
                    speech=result.get("speech"))

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
        viewer.play(wav, answer, emotion=emotion, actions=actions or [])
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
