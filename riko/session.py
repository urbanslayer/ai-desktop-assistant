"""One conversation, driven from anywhere: the terminal, the desktop app, or riko-ctl.

Turns (typed text or a recorded clip) go into a queue and a single worker
thread handles them in order:

    clip -> asr.transcribe -> text -> llm.reply -> tts.speak -> viewer.play

Every step is reported to the avatar page as a UI event, so the desktop
window can show "listening...", "thinking...", and her speech bubble.
"""
import queue
import threading

from riko import asr, llm, tts, viewer


class Session:
    def __init__(self, cfg):
        self.cfg = cfg
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
        llm.forget(self.cfg)
        viewer.emit("say", text="...Who are you again, senpai?")
        print("[llm] chat history cleared")

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
        answer = llm.reply(text, self.cfg)
        print(f"Riko: {answer}")
        wav = tts.speak(answer, self.cfg)
        viewer.emit("status", state="speaking")
        viewer.play(wav, answer)
