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

from riko import asr, hub_client, tts, viewer


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
        threading.Thread(target=self._worker, daemon=True, name="riko-session").start()

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

    def submit_audio(self, wav_bytes):
        self._turns.put(("audio", wav_bytes))

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
        try:
            self.recorder.start()
        except Exception as e:  # no mic, device busy...
            print(f"[asr] can't open the mic: {e}")
            viewer.emit("error", text=f"Mic error: {e}")
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
            return
        if clip is None:
            viewer.emit("status", state="idle")
            return
        self.submit_audio(clip)

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
            try:
                self._turn(kind, payload)
            except Exception as e:  # one bad turn should not end the chat
                print(f"[error] {type(e).__name__}: {e}")
                viewer.emit("error", text=f"{type(e).__name__}: {e}")
            finally:
                if self._turns.empty() and not self.recorder.recording:
                    viewer.emit("status", state="idle")

    def _turn(self, kind, payload):
        if kind == "greet":
            viewer.emit("status", state="thinking")
            greeting = payload.get("greeting") or f"{payload['name']} here."
            self._speak(greeting, audio_url=self._greeting_audio_url(payload), emotion="happy")
            return
        if kind == "audio":
            viewer.emit("status", state="hearing")
            text = asr.transcribe(payload, self.cfg)
            if not text:
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
        self._speak(result["reply"], audio_url=result.get("audio_url"),
                    emotion=result.get("emotion") or "neutral")

    def _speak(self, answer, audio_url=None, emotion="neutral"):
        persona = hub_client.active_persona() or {}
        print(f"{persona.get('name', 'Riko')}: {answer}")
        if self.cfg["tts"] == "hub":
            wav = self._hub_audio(audio_url)
        else:
            wav = tts.speak(answer, self.cfg, voice=persona.get("tts_voice") or None)
        viewer.emit("status", state="speaking")
        viewer.play(wav, answer, emotion=emotion)

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
