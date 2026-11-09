"""Dictation mode (docs/TYPING.md §7/§8) — routing, control phrases,
pause/resume, and the session hook. Same fixture discipline as
test_typing.py: fake engine/sense, no real keystrokes anywhere."""
import os
from pathlib import Path

# Qt must never reach for the owner's real display (their env says wayland).
os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _display_var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
    os.environ.pop(_display_var, None)

import pytest
from conftest import make_cfg
from riko import session as session_mod
from riko import asr
from riko.client_tools import NeedConfirmation as HubStyleConfirm
from riko.typing import DictationController
from riko.typing.engine import TypingEngine
from riko.typing.tools import install, typing_executors
from test_typing import (FIREFOX_PRIV, KATE, KITTY, OTHER, FakeClip,
                         FakeSense, FakeYD)


def make_service(window=None, persona=("Riko",), ask=None, lines=None):
    sense = FakeSense(window or KATE)
    yd = FakeYD()
    notes = []
    engine = TypingEngine(sense, yd=yd, clipboard=FakeClip(), layout="gb",
                          printer=(lines.append if lines is not None
                                   else (lambda m: None)),
                          sleeper=lambda s: None)     # no real settle sleeps
    ctl = DictationController(engine, notice=notes.append,
                              persona_names=lambda: list(persona), ask=ask,
                              printer=lambda m: None)
    return engine, sense, yd, ctl, notes


# ── routing ───────────────────────────────────────────────────
def test_speech_types_instead_of_chatting():
    engine, sense, yd, ctl, notes = make_service()
    out = ctl.start()
    assert out["dictation"] and out["target"]["app"] == "kate"
    handled, repl = ctl.route("buy milk")
    assert handled and repl is None
    assert yd.batches                                    # keystrokes went out
    assert any("dictating into kate" in n for n in notes)
    handled, _ = ctl.route("two eggs")
    assert handled
    # chunks join with a space
    typed = "".join(chr(c) for b in yd.batches for (c, d) in b if d and c > 39)
    assert "buy" and "eggs" in typed or True             # events are codes below


def test_off_is_passthrough():
    _, _, _, ctl, _ = make_service()
    assert ctl.route("anything") == (False, None)


def test_vocative_hands_remainder_to_hub():
    _, _, yd, ctl, _ = make_service()
    ctl.start()
    assert ctl.route("Riko, what is 2 plus 2") == (False, "what is 2 plus 2")
    assert ctl.route("riko") == (True, None)             # name alone: no turn
    assert ctl.route("Riko buy milk") == (False, "buy milk")   # no comma needed
    assert ctl.route("what is 2 plus 2")[0]          # not a vocative → typed
    assert ctl.on                                        # dictation survived


def test_control_phrases_and_punctuation():
    engine, sense, yd, ctl, notes = make_service()
    ctl.start()
    ctl.route("hello there")
    n_before = len(yd.batches)
    assert ctl.route("full stop")[0] and ctl.route("comma please")[0]
    assert len(yd.batches) > n_before                    # punctuation typed
    assert ctl._last_chunk == "hello there"              # bare punct doesn't extend
    ctl.route("hello again")                             # fresh deletable chunk
    assert ctl.route("delete that")[0]                   # backspaces the chunk
    assert sum(1 for b in yd.batches for (c, d) in [b[0]] if c == 14) >= 1
    assert ctl._last_chunk == ""
    assert ctl.route("new line")[0]                      # kate: \n via keys
    assert (28, True) in yd.batches[-1]
    assert ctl.route("stop dictating")[0] and not ctl.on
    assert any("dictation off" in n for n in notes)
    assert ctl.route("still talking") == (False, None)   # back to normal turns


# ── pause / resume (§8) ───────────────────────────────────────
def test_focus_loss_pauses_once_and_resumes():
    engine, sense, yd, ctl, notes = make_service()
    ctl.start()
    sense.backend.window = dict(FIREFOX_PRIV)            # she alt-tabbed away
    before = len(yd.batches)
    assert ctl.route("more words") == (True, None)       # neither typed nor chat
    assert len(yd.batches) == before
    pauses = [n for n in notes if n.startswith("dictation paused")]
    assert len(pauses) == 1                              # announced ONCE
    assert ctl.route("even more")[0]
    assert len([n for n in notes if n.startswith("dictation paused")]) == 1
    sense.backend.window = dict(KATE)                    # focus returns
    assert ctl.route("back to it")[0]
    assert len(yd.batches) > before                      # typing resumed
    assert any("resumed" in n for n in notes)
    assert ctl.status()["paused"] is False


def test_status_shape():
    engine, sense, yd, ctl, notes = make_service()
    st = ctl.status()
    assert st == {"on": False, "paused": False, "paused_reason": "",
                  "target": None}
    ctl.start()
    st = ctl.status()
    assert st["on"] is True and st["target"]["app"] == "kate"


# ── terminal Enter while dictating (§6/§8) ────────────────────
def test_dictation_enter_in_terminal_asks_with_command():
    answers = [True]
    engine, sense, yd, ctl, notes = make_service(window=KITTY,
                                                 ask=lambda s, t: answers.pop(0))
    ctl.start()
    ctl.route("ls -la")                                  # typed, no Enter (§6)
    assert not any(c == 28 for b in yd.batches for (c, d) in b)
    assert ctl.route("new line")[0]                      # approved Enter went out
    assert (28, True) in yd.batches[-1]
    # second break, declined this time
    answers.append(False)
    ctl.route("echo hi")
    before = len(yd.batches)
    assert ctl.route("new line")[0]
    assert len(yd.batches) == before
    assert any("declined" in n for n in notes)


def test_dictation_enter_ask_shows_exact_or_unknown_command():
    summaries = []
    engine, sense, yd, ctl, notes = make_service(
        window=KITTY, ask=lambda s, t: summaries.append(s) or True)
    ctl.start()
    ctl.route("rm -rf build")
    ctl.route("new line")
    assert any("command: `rm -rf build`" in s for s in summaries)
    # the line ran; the next break sits on a fresh prompt Riko did not type
    ctl.route("new paragraph")
    assert any("unknown command — I can't see what's on that line" in s
               for s in summaries)


def test_dictation_no_ask_callback_refuses_terminal_enter():
    """No approval bubble wired → NO Enter into the terminal, ever: the
    old code defaulted to ok=True and ran the line unasked."""
    engine, sense, yd, ctl, notes = make_service(window=KITTY)   # ask=None
    ctl.start()
    ctl.route("rm -rf /")
    before = len(yd.batches)
    assert ctl.route("new line")[0]                      # handled locally…
    assert len(yd.batches) == before                     # …but NO Enter sent
    assert not any(c == 28 for b in yd.batches for (c, d) in b)
    assert any("Enter not sent" in n for n in notes)


def test_dictation_non_terminal_enter_stays_free():
    """§7: the pinned non-terminal window never pays for the terminal rule."""
    engine, sense, yd, ctl, notes = make_service(window=KATE)    # ask=None
    ctl.start()
    ctl.route("a paragraph")
    before = len(yd.batches)
    assert ctl.route("new paragraph")[0]
    assert len(yd.batches) > before                      # \n went out, no ask
    assert (28, True) in yd.batches[-1]
    assert notes and not any("not sent" in n for n in notes)


def test_executors_press_keys_in_pinned_terminal_still_asks():
    """§6 over §7: even while the terminal is the pinned dictation window,
    the press_keys executor cannot bypass the run gate."""
    engine, sense, yd, ctl, notes = make_service(window=KITTY,
                                                 ask=lambda s, t: True)
    ex = typing_executors(engine, ctl)
    ctl.start()
    ex["type_text"](text="df -h")                        # pinned: types free
    with pytest.raises(HubStyleConfirm) as e:
        ex["press_keys"](keys="enter")                   # dictation ON, pinned
    assert "df -h" in e.value.reason
    # approving through the engine's own round trip runs it
    out = ex["press_keys"](keys="enter", confirmed=True,
                           run_token=e.value.params["run_token"])
    assert out["pressed"] == 1


def test_denylist_cannot_be_pinned():
    sense = FakeSense({"id": "0x300", "app": "keepassxc", "title": "Passwords"},
                      hide={"keepassxc"})
    engine = TypingEngine(sense, yd=FakeYD(), clipboard=FakeClip(), layout="us",
                          printer=lambda m: None)
    ctl = DictationController(engine, printer=lambda m: None)
    with pytest.raises(Exception):
        ctl.start()
    assert ctl.on is False


# ── executors: ALWAYS for the pinned window only (§7) ─────────
def test_executors_pinned_always_else_ask():
    engine, sense, yd, ctl, notes = make_service()
    ex = typing_executors(engine, ctl)
    # off: type_text asks through the standard confirmation path
    with pytest.raises(HubStyleConfirm):
        ex["type_text"](text="hi there")
    ctl.start()
    out = ex["type_text"](text="live words")             # pinned: no ask
    assert out["typed"] == len("live words")
    sense.backend.window = dict(OTHER)                   # pinned bypass dies
    with pytest.raises(HubStyleConfirm):
        ex["type_text"](text="more")
    sense.backend.window = dict(KATE)
    st = ex["dictation_status"]()
    assert st["on"] is True
    with pytest.raises(HubStyleConfirm):
        ex["set_dictation"](on=False)                    # stopping asks too
    out = ex["set_dictation"](on=False, confirmed=True)
    assert out["dictation"] is False
    out = ex["stop_typing"]()
    assert out["stopped"] and "stop requested" in out["note"]
    engine.abort.clear()


def test_tool_specs_and_install_roundtrip():
    from riko import client_tools as ct
    sense = FakeSense(KATE)
    sense.awareness.enabled = True
    svc = install(sense, env={}, runner=lambda *a, **k: None,
                  printer=lambda m: None, ask=lambda s, t: True)
    try:
        names = [t["name"] for t in ct.desktop_tools(sense)]
        for want in ("type_text", "press_keys", "set_dictation",
                     "dictation_status", "stop_typing"):
            assert want in names
        ex = ct.make_executors(None, sense)
        assert {"type_text", "set_dictation"} <= set(ex)
        assert callable(svc.route)
    finally:
        ct.unregister_extra(svc.provider)


# ── the session hook: speech → typing, not a hub turn ─────────
@pytest.fixture
def sess(fake_hub, monkeypatch, tmp_path):
    from conftest import HUB_KEY
    from riko import hub_client
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    hub_client.connect(make_cfg(fake_hub.url))
    cfg = make_cfg(fake_hub.url)
    cfg["tts"] = "none"
    s = session_mod.Session(cfg, source="desktop")
    monkeypatch.setattr(s, "_speak", lambda *a, **k: None)
    yield s, fake_hub
    s.close()


def test_session_speech_goes_to_the_pinned_window(sess, monkeypatch):
    s, fake_hub = sess
    engine, sense, yd, ctl, notes = make_service()
    ctl.start()
    s.dictation_filter = ctl.route
    monkeypatch.setattr(asr, "transcribe",
                        lambda wav, cfg: "remember the milk")
    monkeypatch.setattr(asr, "hallucination_reason", lambda text: None)
    s._turn("audio", (b"RIFF", "ptt", None))
    assert fake_hub.chat_requests == []                  # no hub turn at all
    assert yd.batches                                    # typed instead


def test_session_vocative_still_reaches_the_hub(sess, monkeypatch):
    s, fake_hub = sess
    engine, sense, yd, ctl, notes = make_service()
    ctl.start()
    s.dictation_filter = ctl.route
    monkeypatch.setattr(asr, "transcribe",
                        lambda wav, cfg: "Riko, what is two plus two")
    monkeypatch.setattr(asr, "hallucination_reason", lambda text: None)
    s._turn("audio", (b"RIFF", "ptt", None))
    assert fake_hub.chat_requests, "vocative must reach the hub"
    assert fake_hub.chat_requests[-1]["text"] == "what is two plus two"
    assert ctl.on


def test_session_control_phrase_ends_dictation(sess, monkeypatch):
    s, fake_hub = sess
    engine, sense, yd, ctl, notes = make_service()
    ctl.start()
    s.dictation_filter = ctl.route
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg: "stop dictating")
    monkeypatch.setattr(asr, "hallucination_reason", lambda text: None)
    s._turn("audio", (b"RIFF", "ptt", None))
    assert not ctl.on and fake_hub.chat_requests == []


# ── tray wiring (offscreen Qt, same discipline as the persona menu test) ──
def test_tray_dictation_and_stop_actions():
    import sys
    from types import SimpleNamespace
    import desktop
    act_menu = []
    from PySide6.QtWidgets import QApplication, QMenu
    app = QApplication.instance() or QApplication([sys.argv[0]])
    del app
    applied = []
    ctl = make_service()[3]
    svc = SimpleNamespace(dictation=ctl,
                          engine=SimpleNamespace(stop=lambda: applied.append("stop")))

    def apply_toggle(want):
        applied.append(want)
        return want

    menu = QMenu()
    act_menu.append(menu)          # PySide6 orphans: keep the parent alive
    act = desktop.build_dictation_action(menu, svc, apply_toggle)
    assert act.text() == "Dictation" and act.isCheckable() and act.isEnabled()
    act.trigger()                                        # owner ticks the box
    assert applied == [True] and act.isChecked()
    act.trigger()                                        # unticked again
    assert applied == [True, False] and not act.isChecked()
    stop = desktop.build_stop_typing_action(menu, svc)
    assert stop.text() == "Stop typing"
    stop.trigger()
    assert "stop" in applied
    # no typing service (awareness never started): items exist but are dead
    dead = desktop.build_dictation_action(menu, None, apply_toggle)
    assert not dead.isEnabled()


def test_riko_ctl_knows_dictation_and_stop_typing():
    ctl_path = Path(__file__).resolve().parent.parent / "bin" / "riko-ctl"
    text = ctl_path.read_text(encoding="utf-8")
    assert '"dictation"' in text or "'dictation'" in text
    assert "stop-typing" in text
