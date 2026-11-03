"""One conversation, driven from anywhere: the terminal, the desktop app, or riko-ctl.

Turns (typed text, a recorded clip, or a persona switch) go into a queue and
a single worker thread handles them in order:

    clip -> asr.transcribe -> text -> hub.chat -> tts.speak -> viewer.play

Every step is reported to the avatar page as a UI event, so the desktop
window can show "listening...", "thinking...", her speech bubble, and swap
the VRM when the persona changes — by keyword, riko-ctl or another client.
"""
import queue
import threading

from riko import asr, hub_client, tts, viewer


class Session:
    def __init__(self, cfg, source="desktop"):
        self.cfg = cfg
        self.source = source  # stamped on every message the hub stores
        self.hub = hub_client.get()
        hub_client.note_active(self.hub.state())  # who is active right now?
        self.recorder = asr.Recorder()
        self._turns = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name="riko-session").start()

    # ── Ways in ──────────────────────────────────────────────
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
            self._speak(greeting)
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
        self._speak(result["reply"])

    def _speak(self, answer):
        persona = hub_client.active_persona() or {}
        print(f"{persona.get('name', 'Riko')}: {answer}")
        wav = tts.speak(answer, self.cfg, voice=persona.get("tts_voice") or None)
        viewer.emit("status", state="speaking")
        viewer.play(wav, answer)

    def _active_id(self):
        active = hub_client.active_persona() or {}
        return active.get("id")
