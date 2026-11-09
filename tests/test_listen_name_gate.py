"""Name-gate tests (docs/BACKLOG.md Planned 5): always-listen only answers
when the active persona's name / keyword / alias is said, with a short
follow-up window, any-persona addressing (switch + route), the tray checkbox
(offscreen Qt), and the riko-ctl toggle.

Fakes only: fake hub, fake AlwaysListener, fake transcription. Nothing here
opens a real audio device, and the Qt parts run on the offscreen platform
with the display env popped (assigned BEFORE Qt is imported — the owner's
shell exports wayland, and a setdefault would leave it and abort Qt).
"""
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _display_var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
    os.environ.pop(_display_var, None)

from conftest import free_port, make_cfg, persona, wav_bytes  # noqa: E402

from riko import appstate, asr, hub_client, name_gate, session, viewer  # noqa: E402
from test_listen_mode import GOOD, FakeListener  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CTL = ROOT / "bin" / "riko-ctl"

VOICED = {"voiced_seconds": 0.8, "mean_prob": 0.95}


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def make_gate_hub(fake_hub):
    """A persona roster that works for EVERY persona, not one hard-coded
    name: Furina, Nicole (keyword 'nick'), Asuka (from the base fake) and an
    invented multi-word name."""
    fake_hub.personas["furina"] = persona("furina", "Furina", "furina", "Furina here.")
    fake_hub.personas["nicole"] = persona("nicole", "Nicole", "nicole", "Nicole here.")
    fake_hub.personas["nicole"]["keywords"] = ["nicole", "nick"]
    fake_hub.personas["sparkles"] = persona("sparkles", "Mister Sparkles", "sparkles",
                                            "Sparkles online.")
    fake_hub.active = "furina"
    return fake_hub


class GateEnv:
    def __init__(self, cfg, url, listeners, texts, hub):
        self.cfg, self.url, self.listeners, self.texts, self.hub = \
            cfg, url, listeners, texts, hub

    def listener(self):
        return self.listeners[-1]


@pytest.fixture
def gate_env(monkeypatch, fake_hub, tmp_path):
    """Viewer + fake hub (Furina/Nicole/Asuka/Mister Sparkles) + fake
    AlwaysListener + fake transcription. texts['t'] is what the fake ASR
    'hears' on the next clip."""
    make_gate_hub(fake_hub)
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("RIKO_NAME_GATE", raising=False)
    cfg = make_cfg(fake_hub.url)
    cfg["desktop"] = dict(cfg["desktop"], listen_mode="always")
    server = viewer.start(cfg)
    hub_client.connect(cfg)
    hub_client.note_active(hub_client.get().state())
    url = f"http://127.0.0.1:{port}"
    requests_get(url)

    made, texts = [], {"t": "hello there"}

    def factory(on_segment, cfg=None):
        listener = FakeListener(on_segment, cfg)
        made.append(listener)
        return listener

    monkeypatch.setattr(asr, "AlwaysListener", factory)
    monkeypatch.setattr(asr, "transcribe_verbose",
                        lambda wav, cfg=None: (texts["t"], GOOD))
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg=None: texts["t"])
    yield GateEnv(cfg, url, made, texts, fake_hub)
    server.shutdown()
    server.server_close()


def requests_get(url):
    import requests
    requests.get(f"{url}/next", timeout=5)


def drain(url, seconds=0.6):
    """Collect every viewer event the app emits within `seconds`."""
    import requests
    events, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        events += requests.get(f"{url}/next", timeout=5).json()["events"]
        time.sleep(0.05)
    return events


def gate_on(cfg, **listen_opts):
    cfg["listen"] = dict({"name_gate": "name"}, **listen_opts)
    return cfg


def vad_turn(s, text):
    """One VAD utterance straight through the turn path (the fake ASR says
    `text`). Deterministic: runs on the test thread, like the dictation
    session tests do."""
    s._turn("audio", (b"RIFF", "vad", VOICED))


class TestGateMatching:
    """riko/name_gate.py pure matching — every persona, nothing hard-coded."""

    def test_exact_case_and_punctuation_insensitive(self):
        assert name_gate.mentions("FURINA, what's the time?", ["Furina"])
        assert name_gate.mentions("so... furina, hi", ["Furina"])
        assert not name_gate.mentions("the weather is nice", ["Furina"])
        # tokenised whole-word: a name never matches INSIDE another word...
        assert not name_gate.mentions("tsunami season", ["Furina"])

    def test_fuzzy_names(self):
        # the gate's own fuzzy layer, independent of asr.corrections
        assert name_gate.mentions("furrina are you there", ["Furina"])
        assert name_gate.mentions("hey Nicole are you there", ["Nicole"])
        assert name_gate.mentions("azuka what's up", ["Asuka"])
        assert not name_gate.mentions("marina what's up", ["Furina"])
        assert not name_gate.mentions("the farm is big", ["Furina"])

    def test_multiline_and_multiword_names(self):
        names = ["Mister Sparkles"]
        assert name_gate.mentions("mister sparkles wake up", names)
        assert name_gate.mentions("hey Mister Sparkles, hello!", names)
        assert name_gate.mentions("ok mister sparkels are you up", names)  # fuzzy window
        assert not name_gate.mentions("sparkles, mister of the house", names)  # order
        assert not name_gate.mentions("mister who", names)

    def test_keyword_and_alias_names(self):
        assert name_gate.mentions("nick, what's the weather", ["Nicole", "nick"])
        assert name_gate.mentions("Nikkles, are you there", ["Nicole", "Nikkles"])

    def test_find_mention_prefers_active(self):
        entries = [{"id": "a", "name": "Furina", "names": ["Furina"], "active": True},
                   {"id": "b", "name": "Nicole", "names": ["Nicole"], "active": False}]
        active, other = name_gate.find_mention("Furina, ask Nicole to help", entries)
        assert active and other is None  # talk to the host, not the mentioned guest
        active, other = name_gate.find_mention("Nicole, what's up", entries)
        assert active is None and other["id"] == "b"
        active, other = name_gate.find_mention("what time is it", entries)
        assert active is None and other is None

    def test_persona_names_dedup(self):
        p = {"name": "Furina", "keywords": ["furina", "Fury"]}
        assert name_gate.persona_names(p, ["FURI", "Furina"]) == ["Furina", "Fury", "FURI"] \
            or set(x.lower() for x in name_gate.persona_names(p, ["FURI", "Furina"])) \
            == {"furina", "fury", "furi"}

    def test_follow_window(self):
        w = name_gate.FollowWindow(0.15)
        assert not w.open()
        w.restart()
        assert w.open()
        w.restart()  # every accepted utterance re-arms it
        time.sleep(0.2)
        assert not w.open()
        w.restart()
        w.close()  # persona switch closes it
        assert not w.open()
        off = name_gate.FollowWindow(0)  # follow_up_seconds: 0 disables
        off.restart()
        assert not off.open()

    def test_resolve_initial_gate_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("RIKO_NAME_GATE", raising=False)
        assert name_gate.resolve_initial_gate({}) is False            # default off
        assert name_gate.resolve_initial_gate({"listen": {"name_gate": "name"}}) is True
        assert appstate.save_name_gate(False)
        assert name_gate.resolve_initial_gate({"listen": {"name_gate": "name"}}) is False
        monkeypatch.setenv("RIKO_NAME_GATE", "on")  # env wins over everything
        assert name_gate.resolve_initial_gate({}) is True


class TestGateInSessions:
    def test_gate_off_passes_everything(self, gate_env):
        s = session.Session(gate_env.cfg, source="desktop")  # default: off
        assert s.name_gate is False
        gate_env.texts["t"] = "totally random sentence"
        vad_turn(s, gate_env.texts["t"])
        assert gate_env.hub.chat_requests[-1]["text"] == "totally random sentence"
        s.close()

    @pytest.mark.parametrize("utterance,expect", [
        ("Furina, what's the time", "Furina, what's the time"),      # name first
        ("so furina what is two plus two", "so furina what is two plus two"),
        ("what time is it furina", "what time is it furina"),        # name last
        ("hey furina are you there", "hey furina are you there"),    # 'hey <name>'
        ("furrina are you there", "furrina are you there"),          # gate's fuzzy
        # 'Farina': the corrections layer fixes it before the gate sees it
        ("Farina are you there", "Furina are you there"),
    ])
    def test_name_anywhere_passes_full_text(self, gate_env, monkeypatch,
                                            utterance, expect):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        assert s.name_gate is True
        gate_env.texts["t"] = utterance
        vad_turn(s, utterance)
        # the FULL text goes to the hub: she sees she was addressed
        assert gate_env.hub.chat_requests[-1]["text"] == expect
        s.close()

    def test_keyword_and_alias_route_other_persona(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg, name_aliases={"nicole": ["Nikkles"]})
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "nick, what's the weather"
        vad_turn(s, gate_env.texts["t"])
        assert gate_env.hub.active == "nicole"  # routed via POST /api/active
        assert gate_env.hub.chat_requests[-1]["text"] == "nick, what's the weather"
        assert "[asr] addressed Nicole: switching" in capsys.readouterr().out
        # alias for the same persona: already active now, so it just passes
        gate_env.texts["t"] = "Nikkles, tell me more"
        vad_turn(s, gate_env.texts["t"])
        assert gate_env.hub.active == "nicole"
        assert gate_env.hub.chat_requests[-1]["text"] == "Nikkles, tell me more"
        s.close()

    def test_any_persona_can_be_disabled(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg, name_gate_any_persona=False)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "Nicole, what's the weather"
        vad_turn(s, gate_env.texts["t"])
        assert gate_env.hub.chat_requests == []      # not addressed to me
        assert gate_env.hub.active == "furina"       # and nobody was switched
        assert "ignored (no name)" in capsys.readouterr().out
        s.close()

    def test_multiword_persona_active_side(self, gate_env, monkeypatch):
        gate_env.hub.active = "sparkles"
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        hub_client.note_active(s.hub.state())
        gate_env.texts["t"] = "hey mister sparkels, are you up"
        vad_turn(s, gate_env.texts["t"])
        assert gate_env.hub.chat_requests[-1]["text"] == gate_env.texts["t"]
        s.close()

    def test_no_name_ignored_quietly(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "some tv noise about the weather"
        vad_turn(s, gate_env.texts["t"])
        out = capsys.readouterr().out
        assert "[asr] ignored (no name): some tv noise" in out  # terminal only
        assert gate_env.hub.chat_requests == []                 # never reached the hub
        events = drain(gate_env.url)
        types = [e["type"] for e in events]
        assert "heard" not in types, "ignored utterances must not show a bubble"
        assert "error" not in types
        s.close()

    def test_follow_up_window_accepts_expires_and_restarts(self, gate_env, monkeypatch,
                                                           capsys):
        gate_on(gate_env.cfg, follow_up_seconds=0.5)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "furina, what's the time"
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 1
        gate_env.texts["t"] = "and tomorrow?"          # follow-up, no name: in
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 2
        # a second accepted utterance restarts the window
        time.sleep(0.3)
        gate_env.texts["t"] = "and next week?"         # 0.3 s in, re-armed here
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 3
        time.sleep(0.3)                                 # 0.3 s after the restart
        vad_turn(s, "still talking to myself")         # 0.6 s after the FIRST one
        assert len(gate_env.hub.chat_requests) == 4
        time.sleep(0.7)                                 # now it has really expired
        gate_env.texts["t"] = "anyone there"
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 4
        assert "ignored (no name)" in capsys.readouterr().out
        s.close()

    def test_zero_follow_up_disables_the_window(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg, follow_up_seconds=0)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "furina, what's the time"
        vad_turn(s, gate_env.texts["t"])
        gate_env.texts["t"] = "and tomorrow?"
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 1      # follow-up not accepted
        assert "ignored (no name)" in capsys.readouterr().out
        s.close()

    def test_persona_switch_resets_the_window(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg)  # 12 s window by default
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "furina, what's the time"
        vad_turn(s, gate_env.texts["t"])
        assert s._gate_window.open()
        s.switch_persona("asuka")
        assert not s._gate_window.open()                 # new persona, new name
        gate_env.texts["t"] = "what time is it"
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 1      # the old window is gone
        gate_env.texts["t"] = "asuka, what time is it"   # the NEW name works
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 2
        s.close()

    def test_hub_side_switch_resets_the_window(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "furina, what's the time"
        vad_turn(s, gate_env.texts["t"])
        assert s._gate_window.open()
        gate_env.hub.force_active("asuka")  # a switch made on the web page
        hub_client.note_active(s.hub.state())
        gate_env.texts["t"] = "what time is it"
        vad_turn(s, gate_env.texts["t"])   # the gate notices the new persona...
        assert len(gate_env.hub.chat_requests) == 1   # ...and the window is gone
        assert "ignored (no name)" in capsys.readouterr().out
        gate_env.texts["t"] = "asuka, hi"             # the NEW name works
        vad_turn(s, gate_env.texts["t"])
        assert len(gate_env.hub.chat_requests) == 2
        s.close()

    def test_push_to_talk_bypasses_the_gate(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        gate_env.texts["t"] = "a nameless deliberate recording"
        s._turn("audio", (b"RIFF", "ptt", None))   # pressing the key is intent
        assert gate_env.hub.chat_requests[-1]["text"] == gate_env.texts["t"]
        assert "ignored" not in capsys.readouterr().out
        s.close()

    def test_push_to_talk_mode_never_gated(self, gate_env, monkeypatch):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        gate_env.cfg["desktop"]["listen_mode"] = "push_to_talk"
        s = session.Session(gate_env.cfg, source="desktop")
        assert s.listen_mode == "push_to_talk"
        gate_env.texts["t"] = "nameless in ptt mode"
        vad_turn(s, gate_env.texts["t"])   # even a VAD-shaped source passes...
        assert gate_env.hub.chat_requests[-1]["text"] == gate_env.texts["t"]
        s._turn("text", "typed text needs no name")
        assert gate_env.hub.chat_requests[-1]["text"] == "typed text needs no name"
        s.close()

    def test_typed_text_bypasses_the_gate(self, gate_env, monkeypatch):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        s.submit_text("what is two plus two")
        assert wait_for(lambda: len(gate_env.hub.chat_requests) == 1)
        s.close()

    def test_dictation_runs_before_the_gate(self, gate_env, monkeypatch, capsys):
        gate_on(gate_env.cfg)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        typed = []
        s.dictation_filter = lambda text: (typed.append(text) or (True, None))
        gate_env.texts["t"] = "type everything without any name at all"
        vad_turn(s, gate_env.texts["t"])
        assert typed == ["type everything without any name at all"]  # router first
        assert gate_env.hub.chat_requests == []                     # consumed
        assert "ignored" not in capsys.readouterr().out             # gate never ran
        # the gate runs on the REPLACEMENT text when dictation passes through
        s.dictation_filter = lambda text: (False, "what is two plus two")
        vad_turn(s, "whatever was said")
        assert gate_env.hub.chat_requests == []
        assert "ignored (no name)" in capsys.readouterr().out
        # ...and a replacement WITH the name still reaches the hub
        s.dictation_filter = lambda text: (False, "furina, what is two plus two")
        vad_turn(s, "whatever was said")
        assert gate_env.hub.chat_requests[-1]["text"] == "furina, what is two plus two"
        s.close()

    def test_timer_turns_are_not_gated(self, gate_env, monkeypatch):
        gate_on(gate_env.cfg, follow_up_seconds=0)
        monkeypatch.setenv("RIKO_NAME_GATE", "on")
        s = session.Session(gate_env.cfg, source="desktop")
        spoken = []
        monkeypatch.setattr(s, "_speak", lambda text, **kw: spoken.append(text))
        s._turn("timer", {"reply": "Your tea is ready, senpai."})
        assert spoken == ["Your tea is ready, senpai."]  # no name needed
        assert s._gate_window._until == 0.0              # and it never opens it
        s.close()

    def test_gate_state_persists_and_env_wins(self, gate_env, monkeypatch):
        s = session.Session(gate_env.cfg, source="desktop")
        assert s.name_gate is False
        toggled = []
        s.on_name_gate = toggled.append
        s.set_name_gate(True)
        assert s.name_gate is True and toggled == [True]
        assert appstate.load_name_gate() is True         # remembered
        s.close()
        monkeypatch.delenv("RIKO_NAME_GATE", raising=False)
        s2 = session.Session(gate_env.cfg, source="desktop")
        assert s2.name_gate is True                      # restored on the next start
        s2.close()
        monkeypatch.setenv("RIKO_NAME_GATE", "off")      # the env override wins
        s3 = session.Session(gate_env.cfg, source="desktop")
        assert s3.name_gate is False
        s3.close()
        assert appstate.load_name_gate() is True         # override did not persist


class TestTrayAndCtl:
    def test_tray_checkbox_offscreen(self):
        import desktop
        from PySide6.QtWidgets import QApplication, QMenu
        app = QApplication.instance() or QApplication([sys.argv[0]])
        del app

        class FakeSession:
            def __init__(self):
                self.name_gate = False
                self.listen_mode = "push_to_talk"
                self.toggles = []

            def set_name_gate(self, on):
                self.toggles.append(bool(on))
                self.name_gate = bool(on)

        fs = FakeSession()
        menu = QMenu()
        act = desktop.build_name_gate_action(menu, fs)
        assert act.text() == "Only when I say her name"
        assert act.isCheckable()
        # push-to-talk: the gate cannot bite, so the box is greyed out
        assert not act.isEnabled() and not act.isChecked()
        fs.listen_mode = "always"
        desktop.sync_name_gate_action(act, fs)
        assert act.isEnabled()
        act.trigger()  # the owner ticks the box
        assert fs.toggles == [True] and fs.name_gate is True
        act.trigger()  # unticks it
        assert fs.toggles == [True, False] and fs.name_gate is False
        # a toggle made elsewhere re-ticks the box (Qt's own tick could lie)
        fs.name_gate = True
        desktop.sync_name_gate_action(act, fs)
        assert act.isChecked()
        fs.listen_mode = "push_to_talk"
        desktop.sync_name_gate_action(act, fs)
        assert not act.isEnabled()

    def test_wired_in_desktop_and_ctl(self):
        src = (ROOT / "desktop.py").read_text(encoding="utf-8")
        assert '"name_gate": session.name_gate' in src        # status shows it
        assert 'cmd == "name-gate"' in src                    # riko-ctl handler
        assert "namegate-ui" in src                           # tray tick bridge
        assert "menu.addAction(name_gate_action)" in src      # it is in the menu
        ctl = CTL.read_text(encoding="utf-8")
        assert '"name-gate"' in ctl and '"listen"' in ctl
        assert "listen name-gate" in ctl                      # documented spelling

    def test_riko_ctl_listen_name_gate_posts(self):
        """End to end through the real ctl script: `riko-ctl listen name-gate
        on` must POST /control/name-gate {"enabled": "on"} to the app."""
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append((self.path, body.decode()))
                reply = b'{"ok": true, "name_gate": true, "listen_mode": "always"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "RIKO_URL": f"http://127.0.0.1:{port}"}
            for argv, want in ((["listen", "name-gate", "on"], "on"),
                               (["listen", "name-gate", "off"], "off"),
                               (["name-gate", "on"], "on")):
                r = subprocess.run([sys.executable, str(CTL), *argv],
                                   env=env, capture_output=True, text=True, timeout=15)
                assert r.returncode == 0, r.stderr
                assert '"name_gate": true' in r.stdout  # the reply is printed
            assert [p for p, _ in seen] == ["/control/name-gate"] * 3
            assert seen[0][1] == '{"enabled": "on"}'
            assert seen[1][1] == '{"enabled": "off"}'
        finally:
            httpd.shutdown()
            httpd.server_close()
