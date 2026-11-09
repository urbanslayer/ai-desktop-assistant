"""Typing tools (docs/TYPING.md) — fixture-driven, fake-runner tests. Nothing
here sends real keystrokes: ydotool and the clipboard go through fake runners,
the window manager is a FakeSense fed by plain dicts, and the conftest guard
hard-fails any test that still tries to exec ydotool/ydotoold/wl-copy/wl-paste
or reach the Obsidian REST ports."""
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_cfg  # noqa: F401  (keep imports obvious for readers)
from riko.client_tools import ToolError
from riko.typing import detect_layout, resolve_char
from riko.typing import engine as engine_mod
from riko.typing import ydotool as ydotool_mod
from riko.typing.clipboard import Clipboard
from riko.typing.engine import NeedConfirmation, TypingEngine
from riko.typing.keys import parse_combo, parse_keys
from riko.typing.terminals import is_terminal
from riko.typing.ydotool import OWNER_SETUP, YdotoolBackend

FIX = Path(__file__).parent / "fixtures" / "typing"

KATE = {"id": "0x100", "app": "kate", "title": "notes.md — Kate"}
KITTY = {"id": "0x200", "app": "kitty", "title": "user@arch: ~"}
KEEPASS = {"id": "0x300", "app": "keepassxc", "title": "Passwords"}
FIREFOX_PRIV = {"id": "0x400", "app": "firefox", "title": "search — Private Browsing"}
OTHER = {"id": "0x500", "app": "firefox", "title": "Arch Wiki — Mozilla Firefox"}


# ── fakes ─────────────────────────────────────────────────────
class Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""


class FakeAwareness:
    def __init__(self, hide=()):
        self.hide = set(hide)
        self.enabled = True

    def hides(self, app="", title=""):
        return app in self.hide

    def hides_private(self, title="", url=""):
        return "private browsing" in (title or "").lower()


class FakeBackend:
    name = "hyprland"

    def __init__(self, window=None, runner=None):
        self.window = dict(window or KATE)
        self.runner = runner
        self.focus_calls = []

    def focused(self):
        return dict(self.window)

    def focus_window(self, ref):
        self.focus_calls.append(ref)
        return dict(self.window)


class FakeSense:
    def __init__(self, window=None, hide=(), runner=None):
        self.backend = FakeBackend(window, runner)
        self.awareness = FakeAwareness(hide)

    def focus_window(self, ref):
        return self.backend.focus_window(ref)

    def executors(self):
        return {}


class FakeYD:
    """Stands in for YdotoolBackend: records every batch AND the per-call
    hold_ms (the terminal Enter tap must arrive as a down-hold-up), can act
    mid-send."""

    def __init__(self, on_send=None):
        self.batches = []
        self.holds = []
        self.on_send = on_send

    def require(self):
        return None

    def send_events(self, events, hold_ms=None):
        self.batches.append(list(events))
        self.holds.append(hold_ms)
        if self.on_send:
            self.on_send(self)


class FakeClip:
    def __init__(self, saved="OLD TEXT"):
        self.saved = saved
        self.ops = []

    def tool(self):
        return "wayland"

    def read(self):
        self.ops.append(("read", None))
        return self.saved

    def write(self, text):
        self.ops.append(("write", text))

    def clear(self):
        self.ops.append(("clear", None))


def make_engine(window=None, hide=(), layout="gb", on_send=None, lines=None,
                sleeper=None, clock=None, **engine_kw):
    """FakeSense + FakeYD engine. The sleeper defaults to a no-op so the
    post-approval settle costs nothing here; timing tests inject their own
    fake clock (see test_settle_after_approval_*)."""
    sense = FakeSense(window, hide)
    yd = FakeYD(on_send)
    eng = TypingEngine(sense, yd=yd, clipboard=FakeClip(), layout=layout,
                       printer=(lines.append if lines is not None
                                else (lambda m: None)),
                       sleeper=sleeper or (lambda s: None),
                       clock=clock or time.monotonic, **engine_kw)
    return eng, sense, yd


def codes_of(batch):
    return [(c, d) for (c, d) in batch]


# ── layout tables (§4) ────────────────────────────────────────
def test_gb_vs_us_char_mapping():
    assert resolve_char("gb", '"') == (3, ["shift"])       # " on the 2 key
    assert resolve_char("us", '"') == (40, ["shift"])      # " on the ' key
    assert resolve_char("gb", "@") == (40, ["shift"])      # @ shares the ' key
    assert resolve_char("us", "@") == (3, ["shift"])
    assert resolve_char("gb", "#") == (43, [])             # # beside P, unshifted
    assert resolve_char("us", "#") == (4, ["shift"])
    assert resolve_char("gb", "~") == (43, ["shift"])
    assert resolve_char("us", "~") == (41, ["shift"])
    assert resolve_char("gb", "£") == (4, ["shift"])
    assert resolve_char("us", "£") is None                 # → paste fallback
    assert resolve_char("gb", "\\") == (94, [])            # 102nd key left of Z
    assert resolve_char("us", "\\") == (43, [])
    assert resolve_char("gb", "|") == (94, ["shift"])
    assert resolve_char("us", "|") == (43, ["shift"])
    assert resolve_char("gb", "¬") == (41, ["shift"])
    assert resolve_char("gb", "A") == (30, ["shift"])      # letters: shift+base
    assert resolve_char("gb", "a") == (30, [])


def test_layout_detection_order():
    devices = (FIX / "hypr_devices_gb.json").read_text()
    getopt = (FIX / "hypr_getoption_gb.json").read_text()

    def runner_for(**replies):
        def runner(argv, timeout=None, **kw):
            joined = " ".join(str(a) for a in argv)
            for key, reply in replies.items():
                if key in joined:
                    return reply
            return Done("", 1)
        return runner

    lay, src = detect_layout("hyprland",
                             runner=runner_for(devices=Done(devices)), env={})
    assert lay == "gb" and "devices" in src
    lay, src = detect_layout(
        "hyprland", runner=runner_for(getoption=Done(getopt)), env={})
    assert lay == "gb" and "getoption" in src
    lay, src = detect_layout("hyprland", runner=runner_for(), env={},
                             hypr_files=[str(FIX / "hypr_kb" / "hyprland.lua")])
    assert lay == "gb" and "kb_layout" in src
    lay, src = detect_layout("hyprland", runner=runner_for(), env={},
                             hypr_files=["/nonexistent/hyprland.lua"])
    assert lay == "us"                                    # default, never wrong keys
    assert "default" in src

    def gnome_runner(argv, timeout=None, **kw):
        return Done("[('xkb', 'gb'), ('xkb', 'us')]")

    lay, src = detect_layout("gnome", runner=gnome_runner, env={})
    assert lay == "gb" and "gsettings" in src
    assert detect_layout("", runner=None, env={})[0] == "us"


def test_unknown_layout_degrades_loud_not_wrong(tmp_path):
    notes = []
    devices = json.dumps({"keyboards": [
        {"name": "kbd", "main": True, "active_keymap": "Colemak"}]})

    def runner(argv, timeout=None, **kw):
        if "devices" in " ".join(map(str, argv)):
            return Done(devices)
        return Done("", 1)

    lay, src = detect_layout("hyprland", runner=runner, env={}, log=notes.append,
                             home=tmp_path, config_dir=tmp_path,
                             hypr_files=[str(tmp_path / "none.lua")])
    assert lay == "us"
    assert any("no keycode table" in n for n in notes)


# ── terminal identification (§6) ──────────────────────────────
@pytest.mark.parametrize("cls", ["kitty", "foot", "alacritty", "wezterm",
                                 "konsole", "org.kde.konsole", "gnome-terminal-server",
                                 "org.gnome.Ptyxis", "xterm", "ghostty", "tilix",
                                 "com.gexperts.Tilix", "org.wezfurlong.wezterm"])
def test_terminals_recognised(cls):
    assert is_terminal(cls, "")


@pytest.mark.parametrize("cls,title", [
    ("firefox", "Arch Wiki — Mozilla Firefox"),
    ("kate", "notes.md — Kate"),
    ("code", "main.py — Visual Studio Code"),
    ("", "user@arch: ~"),      # a bare shell TITLE alone must not guess
])
def test_non_terminals(cls, title):
    assert not is_terminal(cls, title)


# ── forbidden combos (§5) ─────────────────────────────────────
@pytest.mark.parametrize("combo", ["super+x", "SUPER+x", "hyper+a"])
def test_super_never_passes(combo):
    with pytest.raises(ToolError) as e:
        parse_combo(combo)
    assert "compositor" in str(e.value).lower() or "overview" in str(e.value).lower()


@pytest.mark.parametrize("combo", ["meta", "super"])
def test_lone_super_modifier_refused(combo):
    with pytest.raises(ToolError):
        parse_combo(combo)


@pytest.mark.parametrize("combo", ["ctrl+alt+del", "ctrl+alt+f2", "ctrl+alt+F12",
                                   "ctrl+alt+backspace"])
def test_session_vt_combos_refused(combo):
    with pytest.raises(ToolError):
        parse_combo(combo)


@pytest.mark.parametrize("combo", ["ctrl+s", "ctrl+shift+t", "ctrl+shift+v",
                                   "alt+f4", "enter", "backspace*3"])
def test_allowed_combos_parse(combo):
    mods, base, code, count = parse_combo(combo)
    assert code > 0
    assert count == (3 if "backspace" in combo else 1)


def test_press_keys_forbidden_sends_nothing():
    eng, sense, yd = make_engine()
    with pytest.raises(ToolError):
        eng.press_keys("super+x", confirmed=True)
    assert yd.batches == []                               # refused BEFORE any key


# ── target checks (§2/§3) ─────────────────────────────────────
def test_denylisted_window_refused():
    eng, sense, yd = make_engine(window=KEEPASS, hide={"keepassxc"})
    with pytest.raises(ToolError) as e:
        eng.type_text("secret", confirmed=True)
    assert "denylist" in str(e.value)
    assert yd.batches == []


def test_private_window_refused():
    eng, _, yd = make_engine(window=FIREFOX_PRIV)
    with pytest.raises(ToolError) as e:
        eng.type_text("hello", confirmed=True)
    assert "private" in str(e.value).lower()


def test_auth_agent_refused():
    eng, _, yd = make_engine(window={"id": "0x9", "app": "org.freedesktop.portal.Desktop",
                                     "title": "PolicyKit dialog"})
    with pytest.raises(ToolError):
        eng.type_text("y", confirmed=True)
    assert yd.batches == []


def test_target_mismatch_stops_mid_batch():
    """§1: focus leaving mid-batch stops with NO further keys."""
    eng, sense, yd = make_engine()

    def switch_after_first(y):
        if len(y.batches) == 1:
            sense.backend.window = dict(OTHER)

    yd.on_send = switch_after_first
    with pytest.raises(ToolError) as e:
        eng.type_text("a" * 150, confirmed=True)          # 3 chunks
    assert "changed" in str(e.value)
    assert len(yd.batches) == 1                           # chunk 2 never sent


def test_pinned_target_mismatch_refuses_upfront():
    eng, sense, yd = make_engine()
    with pytest.raises(ToolError) as e:
        eng.type_text("hi", target={"id": "0x999"}, dictation_pinned=True)
    assert "pinned window is not focused" in str(e.value)
    assert yd.batches == []


# ── approval gates (§6/§7) ────────────────────────────────────
def test_type_asks_with_window_and_text():
    eng, _, yd = make_engine()
    with pytest.raises(NeedConfirmation) as e:
        eng.type_text("buy milk")
    assert "kate" in e.value.reason and "buy milk" in e.value.reason
    assert yd.batches == []


def test_terminal_text_single_line_no_control():
    eng, _, yd = make_engine(window=KITTY)
    out = eng.type_text("ls -la\n", confirmed=True)
    assert out["typed"] == 6                              # the \n was stripped
    assert all(c != 28 for (c, d) in yd.batches[0])       # no Enter typed in
    with pytest.raises(ToolError):
        eng.type_text("ls \x1b[31mred", confirmed=True)   # control chars refused


def _approve(eng, keys, window_id=None, kind_first=True):
    """Run the engine's own approval round trip once: capture the token the
    NeedConfirmation issued and re-call press_keys with it (what the runtime
    does after the owner OKs the bubble)."""
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys(keys)
    tok = e.value.run_token
    assert tok, "terminal run/interrupt asks must carry a run_token"
    out = eng.press_keys(keys, confirmed=True, run_token=tok)
    return out


def test_terminal_enter_always_asks_with_the_command():
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("rm -rf / --no-preserve-root", confirmed=True)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    assert "rm -rf / --no-preserve-root" in e.value.reason
    assert len(yd.batches) == 1                           # only the typing so far
    # a generic confirmed=True (hub-approved tool call) must NOT run it:
    # that bubble showed only the args keys='enter', never the command
    with pytest.raises(NeedConfirmation) as e2:
        eng.press_keys("enter", confirmed=True)
    assert "rm -rf / --no-preserve-root" in e2.value.reason
    assert len(yd.batches) == 1
    # the engine's own round trip: token from the ask that showed the
    # command is what grants Enter
    tok = e2.value.run_token
    out = eng.press_keys("enter", confirmed=True, run_token=tok)
    assert out["pressed"] == 1
    assert (28, 1) in yd.batches[-1]
    # replay of a spent token does not re-run anything
    with pytest.raises(NeedConfirmation) as e3:
        eng.press_keys("enter", confirmed=True, run_token=tok)
    assert len(yd.batches) == 2                           # nothing extra sent
    # after Enter the remembered command line is gone (it ran); keys typed
    # via press_keys are tracked too
    eng.press_keys("l", confirmed=True)
    with pytest.raises(NeedConfirmation) as e4:
        eng.press_keys("enter")
    assert "command: `l`" in e4.value.reason


def test_enter_summary_says_unknown_when_riko_did_not_type_it():
    eng, _, yd = make_engine(window=KITTY)                 # fresh prompt
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    assert "unknown command — I can't see what's on that line" in e.value.reason
    assert yd.batches == []
    # the ask still happens (the owner may approve a line she typed herself)


def test_dictation_pin_never_grants_terminal_enter():
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("sudo reboot", confirmed=True)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter", dictation_pinned=True)
    assert "sudo reboot" in e.value.reason
    assert len(yd.batches) == 1                           # Enter NOT sent


def test_run_token_expiry_and_wrong_window_refused():
    import time as _t
    eng, sense, yd = make_engine(window=KITTY)
    eng.type_text("git push", confirmed=True)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    tok = e.value.run_token
    # expiry: age the grant out, then it buys nothing
    eng._run_tokens[tok]["expires"] = _t.monotonic() - 1
    with pytest.raises(NeedConfirmation):
        eng.press_keys("enter", confirmed=True, run_token=tok)
    # wrong window: a token issued for one terminal must not run another
    with pytest.raises(NeedConfirmation) as e2:
        eng.press_keys("enter")
    tok2 = e2.value.run_token
    sense.backend.window = {"id": "0x201", "app": "kitty", "title": "user@arch: /tmp"}
    with pytest.raises(NeedConfirmation) as e2b:         # token is window-bound
        eng.press_keys("enter", confirmed=True, run_token=tok2)
    assert "unknown command" in e2b.value.reason          # other prompt, not run
    # command changed under the approval: re-ask, never run blind
    sense.backend.window = dict(KITTY)
    with pytest.raises(NeedConfirmation) as e3:
        eng.press_keys("enter")
    tok3 = e3.value.run_token
    eng.type_text(" --force", confirmed=True)              # line changed
    with pytest.raises(NeedConfirmation) as e4:
        eng.press_keys("enter", confirmed=True, run_token=tok3)
    assert "--force" in e4.value.reason                    # fresh ask shows it
    # a blank/garbage token is simply no grant
    with pytest.raises(NeedConfirmation):
        eng.press_keys("enter", confirmed=True, run_token="nonsense")


@pytest.mark.parametrize("combo,codes", [
    ("enter", [(28, 1)]),
    ("kp_enter", [(96, 1)]),
    ("ctrl+m", [(29, 1), (50, 1)]),        # CR
    ("ctrl+j", [(29, 1), (36, 1)]),        # LF
    ("shift+enter", [(42, 1), (28, 1)]),
    ("alt+enter", [(56, 1), (28, 1)]),
    ("ctrl+enter", [(29, 1), (28, 1)]),
])
def test_every_terminal_run_key_asks_then_needs_token(combo, codes):
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("echo hi", confirmed=True)
    n0 = len(yd.batches)
    for kw in ({"confirmed": True}, {"dictation_pinned": True}):
        with pytest.raises(NeedConfirmation) as e:
            eng.press_keys(combo, **kw)
        assert "echo hi" in e.value.reason
        assert "press Enter in terminal" in e.value.reason
        assert len(yd.batches) == n0
    out = _approve(eng, combo)
    assert out["pressed"] == 1
    batch = yd.batches[-1]
    for c, d in codes:
        assert (c, d) in batch
    # the line ran: next Enter is a fresh prompt, unknown command
    with pytest.raises(NeedConfirmation) as e2:
        eng.press_keys("enter")
    assert "unknown command" in e2.value.reason


@pytest.mark.parametrize("combo,code", [
    ("ctrl+c", 46), ("ctrl+d", 32), ("ctrl+z", 44), ("ctrl+\\", 43),
])
def test_terminal_interrupt_keys_get_their_own_ask(combo, code):
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("sleep 100", confirmed=True)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys(combo, confirmed=True)              # hub approval ≠ grant
    assert "interrupt" in e.value.reason
    assert "kitty" in e.value.reason                       # target shown
    assert "sleep 100" in e.value.reason
    assert len(yd.batches) == 1
    out = _approve(eng, combo)
    assert out["pressed"] == 1
    assert (29, 1) in yd.batches[-1] and (code, 1) in yd.batches[-1]


def test_non_terminal_keys_unchanged():
    eng, _, yd = make_engine(window=KATE)
    # outside terminals the generic approval still works for everything
    assert eng.press_keys("ctrl+c", confirmed=True)["pressed"] == 1
    assert eng.press_keys("enter", confirmed=True)["pressed"] == 1
    assert eng.press_keys("shift+enter", dictation_pinned=True)["pressed"] == 1
    assert (28, 1) in yd.batches[-1]


def test_hub_approved_press_keys_round_trips_run_token():
    """End to end through the runtime: a hub-approved press_keys('enter')
    (whose bubble shows only the args) STILL asks a second time with the
    exact command, and only that approval runs it."""
    from types import SimpleNamespace
    from riko import client_tools as ct
    from riko.typing.tools import typing_executors

    eng, sense, yd = make_engine(window=KITTY)
    eng.type_text("sudo reboot now", confirmed=True)
    ctl = SimpleNamespace(on=False, pinned=None, paused_reason="")
    ex = typing_executors(eng, ctl)

    seen = {}

    class HubC:
        def client_result(self, cid, rid, **kw):
            seen["result"] = kw

    asked = []
    rt = object.__new__(ct.ClientRuntime)
    rt.executors = {"press_keys": ex["press_keys"]}
    rt.hub = HubC()
    rt.client_id = "c"
    rt.approval_timeout = 10.0
    rt.ask_approval = lambda summary=None, timeout=None: asked.append(summary) or True
    ct.ClientRuntime._dispatch_request(
        rt, {"request_id": "r1", "tool": "press_keys",
             "arguments": {"keys": "enter"}, "needs_approval": True,
             "summary": "run press_keys on pc"}, 10.0)
    # first ask: the generic hub summary; second: the command itself
    assert asked[0] == "run press_keys on pc"
    assert any("sudo reboot now" in a for a in asked[1:])
    assert seen["result"]["ok"] is True
    assert (28, 1) in yd.batches[-1]
    n_batches = len(yd.batches)
    # denying the command-level ask sends NOTHING into the terminal
    eng.type_text("ls", confirmed=True)
    rt.ask_approval = (lambda summary=None, timeout=None:
                       asked.append(summary)
                       or "press Enter in terminal" not in str(summary))
    ct.ClientRuntime._dispatch_request(
        rt, {"request_id": "r2", "tool": "press_keys",
             "arguments": {"keys": "enter"}, "needs_approval": True,
             "summary": "run press_keys on pc"}, 10.0)
    assert seen["result"]["ok"] is False
    assert len(yd.batches) == n_batches + 1                # only the ls typing


def test_non_terminal_enter_needs_no_command_summary():
    eng, _, yd = make_engine(window=KATE)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    assert "press enter" in e.value.reason.lower()
    eng.press_keys("enter", confirmed=True)
    assert (28, 1) in yd.batches[-1]


# ── post-approval settle (owner failure 2026-11-09) ────────────
class FakeClock:
    """sleeper + clock twin: sleep() advances the fake monotonic, so the
    settle and the focus poll run deterministically without wall time."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    def now(self):
        return self.t


def test_approved_call_settles_refocuses_and_logs_timings():
    clk = FakeClock()
    lines = []
    eng, sense, yd = make_engine(window=KITTY, sleeper=clk.sleep,
                                 clock=clk.now, lines=lines)
    out = eng.type_text("df -h", confirmed=True)          # post-approval call
    assert out["typed"] == 5
    # release wait, then two consecutive focused() reads 50 ms apart
    assert clk.slept[:3] == pytest.approx([0.35, 0.05, 0.05])
    assert sense.backend.focus_calls[-1] == "0x200"       # focus re-issued
    assert any("settle" in ln and "focus" in ln for ln in lines)


def test_run_token_enter_settles_before_injection():
    clk = FakeClock()
    eng, sense, yd = make_engine(window=KITTY, sleeper=clk.sleep,
                                 clock=clk.now)
    eng.type_text("df -h", confirmed=True)                # line on the prompt
    n_before = len(clk.slept)
    out = _approve(eng, "enter")                          # engine's own round trip
    assert out["pressed"] == 1
    assert clk.slept[n_before] == pytest.approx(0.35)     # settle first…
    assert sense.backend.focus_calls[-1] == "0x200"       # …then refocus…
    assert (28, 1) in yd.batches[-1]                      # …only then the keys


def test_settle_gives_up_when_focus_never_returns():
    clk = FakeClock()
    eng, sense, yd = make_engine(window=KITTY, sleeper=clk.sleep,
                                 clock=clk.now)
    with pytest.raises(NeedConfirmation):
        eng.type_text("df -h")                            # ask first (no settle)

    def steal(ref):                                       # something grabs focus
        sense.backend.window = dict(OTHER)
        return dict(OTHER)

    sense.backend.focus_window = steal
    with pytest.raises(ToolError) as e:
        eng.type_text("df -h", confirmed=True)
    assert "could not give the terminal focus back" in str(e.value)
    assert yd.batches == []                               # nothing was typed
    # waited the full ~1.5 s focus_wait budget at the 50 ms poll cadence
    assert clk.slept[0] == pytest.approx(0.35)
    assert all(s == pytest.approx(0.05) for s in clk.slept[1:])
    assert 29 <= len(clk.slept) - 1 <= 31


def test_focus_poll_needs_two_consecutive_reads():
    """A single read catching the target mid-switch must not be enough."""
    clk = FakeClock()
    eng, sense, yd = make_engine(window=KATE, sleeper=clk.sleep,
                                 clock=clk.now)
    seq = [dict(KATE),                       # resolve_target's read
           dict(OTHER), dict(KATE),          # one good read: not enough
           dict(KATE)]                       # second consecutive read: go
    sense.backend.focused = lambda: dict(seq.pop(0)) if seq else dict(KATE)
    out = eng.type_text("hi", confirmed=True)
    assert out["typed"] == 2                                # typed after 3 polls
    # settle wait, then one 50 ms pause per focused() read (sleep-then-read)
    assert clk.slept == pytest.approx([0.35, 0.05, 0.05, 0.05])


def test_dictation_pin_gets_no_settle():
    """Typing into the pinned window was never an approval in her window:
    no settle, no re-focus."""
    eng, sense, yd = make_engine(window=KATE)
    pin = dict(KATE)
    eng.type_text("live words", target=pin, dictation_pinned=True)
    # the pinned route never touched focus at all: no settle, no refocus
    assert sense.backend.focus_calls == []
    assert (28, 1) not in yd.batches[-1]


# ── the injected Enter is a down-hold-up, not a zero-delay tap ──
def test_terminal_enter_taps_with_an_explicit_hold():
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("df -h", confirmed=True)
    _approve(eng, "enter")
    assert yd.batches[-1] == [(28, True), (28, False)]
    assert yd.holds[-1] == 30          # --key-delay=30: down … 30 ms … up
    _approve(eng, "kp_enter")
    assert yd.batches[-1] == [(96, True), (96, False)]
    assert yd.holds[-1] == 30


def test_other_keys_keep_the_standard_tap():
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("sleep 100", confirmed=True)
    _approve(eng, "ctrl+c")                                # interrupt combo
    assert yd.holds[-1] is None
    _approve(eng, "ctrl+enter")                            # modified run key
    assert yd.holds[-1] is None
    eng.press_keys("l", confirmed=True)                    # plain char key
    assert yd.holds[-1] is None
    eng2, _, yd2 = make_engine(window=KATE)
    eng2.press_keys("ctrl+s", confirmed=True)              # non-terminal
    assert yd2.holds[-1] is None


# ── window id formats: one window, one key (bug 2) ─────────────
def test_window_id_normalisation_keeps_the_typed_line():
    """hyprctl prints the same address in different shapes depending on the
    path (clients vs activewindow vs a model-echoed ref); the line typed
    into that kitty must still be found when the Enter ask fires."""
    upper = {"id": "0x5642C7991580", "app": "kitty", "title": "user@arch: ~"}
    bare = {"id": "5642c7991580", "app": "kitty", "title": "user@arch: ~"}
    lower = {"id": "0x5642c7991580", "app": "kitty", "title": "user@arch: ~"}
    eng, sense, yd = make_engine(window=upper)
    eng.type_text("df -h", confirmed=True)
    assert list(eng._last_line) == ["0x5642c7991580"]      # canonical keys
    sense.backend.window = dict(bare)                      # 0x stripped
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    assert "df -h" in e.value.reason                       # line still found
    # and the token issued for one shape validates under another
    tok = e.value.run_token
    sense.backend.window = dict(lower)
    out = eng.press_keys("enter", confirmed=True, run_token=tok)
    assert out["pressed"] == 1


def test_same_window_and_pins_across_id_shapes():
    a = {"id": "0x5642C7991580", "app": "kitty"}
    b = {"id": " address:0x5642c7991580 ", "app": "kitty"}
    c = {"id": "5642c7991580", "app": "kitty"}
    assert TypingEngine.same_window(a, b) and TypingEngine.same_window(a, c)
    eng, sense, _ = make_engine(window=a)
    eng.type_text("ok", target=c, dictation_pinned=True)   # pinned: no mismatch
    sense.backend.window = {"id": "0x999", "app": "kitty"}
    with pytest.raises(ToolError) as e:
        eng.type_text("x", target=c, dictation_pinned=True)
    assert "pinned window is not focused" in str(e.value)


def test_norm_window_id_passes_non_hex_through():
    from riko.desktop_sense.util import norm_window_id as n
    assert n("0x5642C7991580") == "0x5642c7991580"
    assert n("5642c7991580") == "0x5642c7991580"
    assert n("address:0x5642C7991580") == "0x5642c7991580"
    assert n(" kitty ") == "kitty"          # classes/refs stay as-is
    assert n("12345") == "12345"            # short numeric ids untouched
    assert n(None) == "" and n("") == ""


# ── paste fallback (§3/§4/§5) ─────────────────────────────────
def test_long_text_pastes_and_restores_clipboard():
    eng, _, yd = make_engine(window=KATE)
    eng.clip = FakeClip(saved="OLD TEXT")
    text = "x" * 250
    out = eng.type_text(text, confirmed=True)
    assert out["method"] == "paste" and out["typed"] == 250
    ops = eng.clip.ops
    assert ops[0] == ("read", None)
    assert ops[1] == ("write", text)
    assert ops[-1] == ("write", "OLD TEXT")               # restored, always
    # Ctrl+V went out as the paste combo
    assert (29, 1) in yd.batches[0] and (47, 1) in yd.batches[0]
    assert (42, 1) not in yd.batches[0]                   # no Shift outside terms


def test_terminal_paste_uses_ctrl_shift_v():
    eng, _, yd = make_engine(window=KITTY)
    eng.clip = FakeClip(saved="")
    eng.type_text("y" * 250, confirmed=True)
    assert (42, 1) in yd.batches[0] and (29, 1) in yd.batches[0]
    assert eng.clip.ops[-1] == ("clear", None)            # was empty → cleared


def test_unmappable_char_falls_back_to_paste():
    eng, _, yd = make_engine(window=KATE)                # gb table, short text
    eng.clip = FakeClip(saved="OLD TEXT")
    out = eng.type_text("price → 5 £", confirmed=True)
    assert out["method"] == "paste"


# ── caps, lock, abort (§4) ────────────────────────────────────
def test_cap_4000():
    eng, _, yd = make_engine()
    with pytest.raises(ToolError) as e:
        eng.type_text("x" * 4001, confirmed=True)
    assert "cap" in str(e.value) or "4000" in str(e.value)
    assert yd.batches == []


def test_one_batch_at_a_time():
    eng, _, yd = make_engine()
    assert eng._lock.acquire(timeout=0)
    try:
        with pytest.raises(ToolError) as e:
            eng.type_text("hi", confirmed=True)
        assert "batch" in str(e.value)
    finally:
        eng._lock.release()


def test_abort_mid_batch():
    def abort_after_first(yd):
        if len(yd.batches) == 1:
            yd.eng.abort.set()
    eng, sense, yd = make_engine(on_send=abort_after_first)
    yd.eng = eng
    with pytest.raises(ToolError) as e:
        eng.type_text("a" * 130, confirmed=True)
    assert "aborted" in str(e.value)
    assert len(yd.batches) == 1
    assert eng.stop() and eng.abort.is_set()


# ── backend availability (§2) ─────────────────────────────────
def test_missing_ydotool_shows_owner_setup(monkeypatch):
    monkeypatch.setattr(ydotool_mod.shutil, "which", lambda *a, **k: None)
    yd = YdotoolBackend(runner=lambda *a, **k: Done(""), env={})
    assert "ydotool" in yd.check()
    eng = TypingEngine(FakeSense(), yd=yd, clipboard=FakeClip(), layout="us",
                       printer=lambda m: None)
    with pytest.raises(ToolError) as e:
        eng.type_text("hi", confirmed=True)
    assert "sudo pacman -S ydotool" in str(e.value)
    assert OWNER_SETUP.splitlines()[0] in str(e.value)


def test_custom_socket_dead_is_reported(tmp_path):
    """An explicit YDOTOOL_SOCKET that no one owns must fail up front —
    exactly the half-started-daemon state docs/TYPING.md §2 warns about."""
    env = {"PATH": "/usr/bin:/bin", "YDOTOOL_SOCKET": str(tmp_path / "gone.sock")}
    yd = YdotoolBackend(runner=lambda *a, **k: Done(""), env=env)
    reason = yd.check()
    assert reason and "gone.sock" in reason


# ── socket discovery (§2: systemd user service layout) ────────
class _FakeDaemons:
    """Keeps fake listeners alive for the whole test (GC would close them
    and leave stale files). Default type is SOCK_DGRAM: ydotoold 1.0 binds
    a datagram socket, so that is what the probe must find. listen_stream()
    models a pre-1.0 stream daemon (the fallback path)."""

    def __init__(self):
        self.srvs = []

    def _bind(self, dirpath, name, kind):
        dirpath = Path(dirpath)
        dirpath.mkdir(parents=True, exist_ok=True)
        path = dirpath / name
        srv = socket.socket(socket.AF_UNIX, kind)
        srv.bind(str(path))
        if kind == socket.SOCK_STREAM:
            srv.listen(128)              # dgram needs no backlog; connect-only probes
        self.srvs.append(srv)
        return str(path)

    def listen(self, dirpath, name=".ydotool_socket"):
        return self._bind(dirpath, name, socket.SOCK_DGRAM)

    def listen_stream(self, dirpath, name=".ydotool_socket"):
        return self._bind(dirpath, name, socket.SOCK_STREAM)


@pytest.fixture()
def fake_daemons():
    return _FakeDaemons()


class _RecRunner:
    """Records argv AND the env handed to the runner (ydotool never runs)."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=None, env=None, **kw):
        self.calls.append((list(argv), dict(env or {})))
        return Done("")


@pytest.fixture()
def fake_bins(monkeypatch):
    """Pretend ydotool/ydotoold are installed (which() never hits the box)."""
    monkeypatch.setattr(ydotool_mod.shutil, "which",
                        lambda name, **kw: f"/usr/bin/{name}")


@pytest.fixture()
def sock_root(tmp_path_factory):
    """Short root for AF_UNIX sockets: tmp_path per-test names can exceed
    the 108-char sun_path limit under deep TMPDIRs."""
    return tmp_path_factory.mktemp("y")


def test_env_socket_wins_and_is_passed_to_runner(sock_root, fake_bins,
                                                 fake_daemons):
    env_sock = fake_daemons.listen(sock_root / "envsock")
    fake_daemons.listen(sock_root / "rt")
    env = {"PATH": "/usr/bin:/bin", "YDOTOOL_SOCKET": env_sock,
           "XDG_RUNTIME_DIR": str(sock_root / "rt")}
    yd = YdotoolBackend(runner=(_rec := _RecRunner()), env=env)
    assert yd.check() is None
    assert yd.socket_path() == env_sock                  # explicit override first
    yd.send_events([(30, 1), (30, 0)])
    argv, sent_env = _rec.calls[0]
    assert argv[0] == "ydotool"
    assert sent_env["YDOTOOL_SOCKET"] == env_sock        # explicit, never inherited


def test_xdg_runtime_dir_socket_found(sock_root, fake_bins, fake_daemons):
    """The systemd user service layout: socket at $XDG_RUNTIME_DIR/..."""
    rt_sock = fake_daemons.listen(sock_root / "run")
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(sock_root / "run")}
    yd = YdotoolBackend(runner=(_rec := _RecRunner()), env=env)
    assert yd.check() is None
    assert yd.socket_path() == rt_sock
    yd.send_events([(30, 1)])
    assert _rec.calls[0][1]["YDOTOOL_SOCKET"] == rt_sock


def test_run_user_uid_fallback_when_xdg_unset(sock_root, fake_bins,
                                              fake_daemons, monkeypatch):
    """XDG_RUNTIME_DIR unset → /run/user/<uid>/.ydotool_socket is tried."""
    monkeypatch.setattr(ydotool_mod, "_RUN_USER_BASE", str(sock_root / "runuser"))
    monkeypatch.setattr(ydotool_mod.os, "getuid", lambda: 4242)
    sock = fake_daemons.listen(sock_root / "runuser" / "4242")
    yd = YdotoolBackend(runner=lambda *a, **k: Done(""),
                        env={"PATH": "/usr/bin:/bin"})
    assert yd.check() is None
    assert yd.socket_path() == sock


def test_tmp_socket_is_last_resort(sock_root, fake_bins, fake_daemons,
                                   monkeypatch):
    monkeypatch.setattr(ydotool_mod, "_TMP_SOCKET",
                        str(sock_root / "legacy" / ".ydotool_socket"))
    sock = fake_daemons.listen(sock_root / "legacy")
    empty_rt = sock_root / "empty-rt"
    empty_rt.mkdir()
    yd = YdotoolBackend(runner=lambda *a, **k: Done(""),
                        env={"PATH": "/usr/bin:/bin",
                             "XDG_RUNTIME_DIR": str(empty_rt)})
    assert yd.check() is None                            # runtime dir missed, /tmp hit
    assert yd.socket_path() == sock


def test_stale_socket_skipped_live_one_used(sock_root, fake_bins, fake_daemons,
                                            monkeypatch):
    """A dead (refusing) socket in an earlier slot must not half-type:
    resolution moves on to the next candidate."""
    stale_dir = sock_root / "rt"
    stale_dir.mkdir()
    stale = stale_dir / ".ydotool_socket"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    dead.bind(str(stale))
    dead.close()                                         # file stays → ECONNREFUSED
    monkeypatch.setattr(ydotool_mod, "_TMP_SOCKET",
                        str(sock_root / "legacy" / ".ydotool_socket"))
    (sock_root / "legacy").mkdir()
    live = fake_daemons.listen(sock_root / "legacy")
    yd = YdotoolBackend(runner=lambda *a, **k: Done(""),
                        env={"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(stale_dir)})
    assert yd.socket_path() == live


# ── probe_socket: datagram-first (§2, ydotoold 1.0) ───────────
def test_probe_accepts_datagram_daemon(sock_root):
    """ydotoold 1.0 binds SOCK_DGRAM: a stream-only probe used to get
    EPROTOTYPE there and report the live daemon as [dead]."""
    from riko.typing.ydotool import ALIVE, probe_socket
    daemons = _FakeDaemons()                 # keep alive: GC would close the socket
    sock = daemons.listen(sock_root)
    assert probe_socket(sock) == ALIVE


def test_probe_still_accepts_stream_listener(sock_root):
    """Documented fallback: a pre-1.0 (stream) daemon answers EPROTOTYPE to
    the datagram probe and must still be found alive via the stream retry."""
    from riko.typing.ydotool import ALIVE, probe_socket
    daemons = _FakeDaemons()                 # keep alive: GC would close the socket
    sock = daemons.listen_stream(sock_root)
    assert probe_socket(sock) == ALIVE


def test_probe_stale_socket_files_are_dead(sock_root):
    """Bind-then-close leaves the file; ECONNREFUSED on the connect = dead,
    for either socket type."""
    from riko.typing.ydotool import DEAD, probe_socket
    for kind, name in ((socket.SOCK_DGRAM, "dgram-stale.sock"),
                       (socket.SOCK_STREAM, "stream-stale.sock")):
        path = sock_root / name
        s = socket.socket(socket.AF_UNIX, kind)
        s.bind(str(path))
        s.close()
        assert probe_socket(str(path)) == DEAD


def test_probe_missing_path(sock_root):
    from riko.typing.ydotool import MISSING, probe_socket
    assert probe_socket(str(sock_root / "nope.sock")) == MISSING


def test_missing_paths_error_lists_what_was_tried(sock_root, fake_bins, monkeypatch):
    monkeypatch.setattr(ydotool_mod, "_TMP_SOCKET",
                        str(sock_root / "nowhere" / ".ydotool_socket"))
    rt = sock_root / "rt"
    rt.mkdir()
    yd = YdotoolBackend(runner=lambda *a, **k: Done(""),
                        env={"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(rt)})
    reason = yd.check()
    assert reason and str(rt / ".ydotool_socket") in reason
    assert "nowhere" in reason                           # both tried paths named


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores socket file permissions")
def test_socket_exists_but_denied(sock_root, fake_bins, fake_daemons):
    """EACCES gets its own message: enable-the-service advice would be wrong."""
    sock = fake_daemons.listen(sock_root)
    os.chmod(sock, 0o000)
    try:
        yd = YdotoolBackend(runner=lambda *a, **k: Done(""),
                            env={"PATH": "/usr/bin:/bin",
                                 "XDG_RUNTIME_DIR": str(sock_root)})
        reason = yd.check()
        assert reason and "permission denied" in reason
        assert sock in reason
    finally:
        os.chmod(sock, 0o600)


def test_send_events_refuses_and_names_dead_explicit_socket(sock_root, fake_bins):
    rec = _RecRunner()
    yd = YdotoolBackend(runner=rec, env={
        "PATH": "/usr/bin:/bin",
        "YDOTOOL_SOCKET": str(sock_root / "nobody-home.sock")})
    from riko.typing.ydotool import YdotoolUnavailable
    with pytest.raises(YdotoolUnavailable) as e:
        yd.send_events([(30, 1)])
    assert "nobody-home.sock" in str(e.value)
    assert rec.calls == []                               # nothing was run


# ── argv: ydotool 1.0.4 syntax (§2) ───────────────────────────
def _kbd_backend(fake_bins, fake_daemons, sock_root, runner, **kw):
    """Backend wired to a fake daemon socket; `runner` records/replays."""
    sock = fake_daemons.listen(sock_root / "kbd")
    yd = YdotoolBackend(runner=runner,
                        env={"PATH": "/usr/bin:/bin", "YDOTOOL_SOCKET": sock},
                        **kw)
    return yd, sock


def test_send_events_argv_matches_ydotool_104_syntax(sock_root, fake_bins,
                                                     fake_daemons):
    """ydotool 1.0.4 has NO global options: the delay is `--key-delay=N` on
    the `key` subcommand and events are `<keycode>:<pressed>` tokens. The
    old `ydotool --keydelay 8 key …` died on the usage text every call."""
    rec = _RecRunner()
    yd, sock = _kbd_backend(fake_bins, fake_daemons, sock_root, rec)
    yd.send_events([(30, True), (30, False), (42, False)])
    assert len(rec.calls) == 1
    argv, sent_env = rec.calls[0]
    assert argv == ["ydotool", "key", "--key-delay=8", "30:1", "30:0", "42:0"]
    assert "--keydelay" not in argv
    assert sent_env["YDOTOOL_SOCKET"] == sock            # env injection kept


def test_key_delay_lands_on_the_key_subcommand(sock_root, fake_bins,
                                               fake_daemons):
    rec = _RecRunner()
    yd, _ = _kbd_backend(fake_bins, fake_daemons, sock_root, rec,
                         key_delay_ms=25)
    yd.send_events([(14, True), (14, False)])
    assert rec.calls[0][0][:3] == ["ydotool", "key", "--key-delay=25"]


def test_hold_ms_overrides_the_delay_for_one_tap(sock_root, fake_bins,
                                                 fake_daemons):
    """The terminal Enter is a down-HOLD-up: ydotool's --key-delay is the
    only dwell knob (bare values like 'aaa' also just delay, but a numeric
    delay token is what the parser documents), so the held tap rides on a
    per-call --key-delay while the global default stays untouched."""
    rec = _RecRunner()
    yd, _ = _kbd_backend(fake_bins, fake_daemons, sock_root, rec)
    yd.send_events([(28, True), (28, False)], hold_ms=30)
    assert rec.calls[0][0] == ["ydotool", "key", "--key-delay=30",
                               "28:1", "28:0"]
    yd.send_events([(28, True), (28, False)])          # next call: default
    assert rec.calls[1][0] == ["ydotool", "key", "--key-delay=8",
                               "28:1", "28:0"]


def test_long_batches_split_at_the_event_cap(sock_root, fake_bins,
                                             fake_daemons, monkeypatch):
    """Over-long batches become several `ydotool key` calls, order kept,
    every call carrying its own --key-delay header."""
    monkeypatch.setattr(ydotool_mod, "MAX_EVENTS_PER_CALL", 4)
    rec = _RecRunner()
    yd, _ = _kbd_backend(fake_bins, fake_daemons, sock_root, rec)
    events = [(30, i % 2 == 0) for i in range(10)]
    yd.send_events(events)
    assert [c[0] for c in rec.calls] == [
        ["ydotool", "key", "--key-delay=8"] + [f"30:{1 if i % 2 == 0 else 0}"
                                               for i in range(4)],
        ["ydotool", "key", "--key-delay=8"] + [f"30:{1 if i % 2 == 0 else 0}"
                                               for i in range(4, 8)],
        ["ydotool", "key", "--key-delay=8"] + [f"30:{1 if i % 2 == 0 else 0}"
                                               for i in range(8, 10)],
    ]


class Done2:
    """Done honours every field (the shared Done fixture pins stderr '')."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_failure_reports_stdout_and_exit_code(sock_root, fake_bins,
                                              fake_daemons):
    """ydotool 1.0.4 prints its usage text on STDOUT: an empty stderr must
    not hide the cause — and the message must not blame socket permissions
    the output never mentioned."""
    def runner(argv, timeout=None, env=None, **kw):
        return Done2(returncode=1, stdout=(
            "Usage: key [OPTION]... [KEYCODES]...\n"
            "  -d, --key-delay=N  Delay N milliseconds between key events\n"))
    yd, _ = _kbd_backend(fake_bins, fake_daemons, sock_root, runner)
    from riko.typing.ydotool import YdotoolUnavailable
    with pytest.raises(YdotoolUnavailable) as e:
        yd.send_events([(30, 1), (30, 0)])
    msg = str(e.value)
    assert "exit code 1" in msg
    assert "Usage: key" in msg                           # stdout surfaced
    assert "permission" not in msg.lower()
    assert "root" not in msg


def test_failure_with_no_output_says_so(sock_root, fake_bins, fake_daemons):
    yd, _ = _kbd_backend(fake_bins, fake_daemons, sock_root,
                         lambda *a, **k: Done2(returncode=7))
    from riko.typing.ydotool import YdotoolUnavailable
    with pytest.raises(YdotoolUnavailable) as e:
        yd.send_events([(30, 1)])
    assert "exit code 7" in str(e.value)
    assert "no output" in str(e.value)


def test_failure_blames_socket_only_when_output_says_so(sock_root, fake_bins,
                                                        fake_daemons):
    yd, _ = _kbd_backend(
        fake_bins, fake_daemons, sock_root,
        lambda *a, **k: Done2(returncode=1,
                              stderr="connect: Permission denied"))
    from riko.typing.ydotool import YdotoolUnavailable
    with pytest.raises(YdotoolUnavailable) as e:
        yd.send_events([(30, 1)])
    assert "socket" in str(e.value) and "root" in str(e.value)


# ── audit (§6) ────────────────────────────────────────────────
def test_audit_line_names_window_not_text():
    lines = []
    eng, _, yd = make_engine(lines=lines, window=KATE)
    eng.type_text("my sesame seed", confirmed=True)
    audit = [l for l in lines if l.startswith("[typing]")]
    assert audit and "kate" in audit[-1] and "notes.md" in audit[-1]
    assert "sesame" not in " ".join(lines) and "sesame" not in audit[-1]


# ── clipboard class plumbing ──────────────────────────────────
def test_clipboard_uses_wl_tools_via_runner(tmp_path, monkeypatch):
    calls = []

    def runner(argv, timeout=None, env=None, input=None, **kw):
        calls.append((list(argv), input))
        if argv[0] == "wl-paste":
            return Done("saved")
        return Done("")

    from riko.typing import clipboard as clip_mod
    monkeypatch.setattr(clip_mod, "shutil", SimpleNamespace(
        which=lambda name, **kw: {"wl-copy": "/usr/bin/wl-copy",
                                  "wl-paste": "/usr/bin/wl-paste"}.get(name)))
    clip = Clipboard(runner=runner, env={"WAYLAND_DISPLAY": "wayland-1"})
    assert clip.tool() == "wayland"
    assert clip.read() == "saved"
    clip.write("hello")
    assert calls[1] == (["wl-copy", "--foreground"], "hello")


# ── extra tools ride the awareness gate (client_tools hook) ───
def test_register_extra_merges_under_gate():
    from types import SimpleNamespace
    from riko import client_tools as ct

    def provider(sense=None):
        spec = {"name": "demo_extra", "risk": "read", "description": "x",
                "parameters": {"type": "object", "properties": {}}}
        return [spec], {"demo_extra": lambda **kw: "done"}

    on = SimpleNamespace(awareness=SimpleNamespace(enabled=True),
                         executors=lambda: {})
    off = SimpleNamespace(awareness=SimpleNamespace(enabled=False),
                          executors=lambda: {})
    ct.register_extra(provider)
    try:
        assert any(t["name"] == "demo_extra" for t in ct.desktop_tools(on))
        assert "demo_extra" in ct.make_executors(None, on)
        assert not any(t["name"] == "demo_extra" for t in ct.desktop_tools(off))
        assert "demo_extra" not in ct.make_executors(None, off)
    finally:
        ct.unregister_extra(provider)
    assert not any(t["name"] == "demo_extra" for t in ct.desktop_tools(on))


def test_hub_approval_injects_confirmed():
    """_dispatch_request must not make the owner approve twice: when the hub
    already asked (needs_approval) and she said yes, the executor gets
    confirmed=True."""
    from riko import client_tools as ct

    seen = {}

    class HubC:
        def client_result(self, cid, rid, **kw):
            seen["result"] = kw

    def fn(text="x", confirmed=False):
        seen["confirmed"] = confirmed
        return "ok"

    rt = object.__new__(ct.ClientRuntime)
    rt.executors = {"t": fn}
    rt.hub = HubC()
    rt.client_id = "c"
    rt.approval_timeout = 10.0
    asked = []
    rt.ask_approval = lambda summary, timeout=None: asked.append(summary) or True
    ct.ClientRuntime._dispatch_request(
        rt, {"request_id": "r1", "tool": "t", "arguments": {"text": "hi"},
             "needs_approval": True, "summary": "type hi"}, 10.0)
    assert asked == ["type hi"] and seen["confirmed"] is True
    assert seen["result"]["ok"] is True
    # without hub approval the executor sees the plain call (it gates itself)
    seen.clear()
    ct.ClientRuntime._dispatch_request(
        rt, {"request_id": "r2", "tool": "t", "arguments": {"text": "hi"},
             "needs_approval": False, "summary": "type hi"}, 10.0)
    assert seen["confirmed"] is False


# ── hub prompt line ───────────────────────────────────────────
def test_prompt_line_only_with_typing_tools():
    from hub.tools.registry import tools_prompt
    with_line = tools_prompt(["pc__desktop_windows", "pc__type_text",
                              "pc__press_keys", "pc__dictation_status"])
    assert "type at the cursor" in with_line
    assert "NEVER pressed" in with_line
    plain = tools_prompt(["pc__desktop_windows"])
    assert "type at the cursor" not in plain


# ── the conftest guard itself ─────────────────────────────────
def test_guard_blocks_real_binaries_and_obsidian():
    with pytest.raises(RuntimeError):
        subprocess.run(["ydotool", "key", "30:1"])
    with pytest.raises(RuntimeError):
        subprocess.run(["ydotoold"], capture_output=True)
    with pytest.raises(RuntimeError):
        subprocess.Popen(["wl-copy", "x"])
    with pytest.raises(RuntimeError):
        subprocess.run(["wl-paste", "-n"])
    with pytest.raises(RuntimeError):
        subprocess.run(["kate", "file.md"])
    for port in (27123, 27124):
        s = socket.socket()
        try:
            with pytest.raises(RuntimeError):
                s.connect(("127.0.0.1", port))
        finally:
            s.close()


def test_guard_blocks_real_ydotool_sockets():
    """The live systemd user service socket must be untouchable in tests —
    with either socket type (ydotoold 1.0 is SOCK_DGRAM) and via sendto,
    which dials an unconnected datagram socket without calling connect()."""
    uid = os.getuid()
    for path in (f"/run/user/{uid}/.ydotool_socket", "/run/user/0/.ydotool_socket",
                 "/tmp/.ydotool_socket", "/run/.ydotool_socket"):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            s = socket.socket(socket.AF_UNIX, kind)
            try:
                with pytest.raises(RuntimeError):
                    s.connect(path)
                with pytest.raises(RuntimeError):
                    s.sendto(b"\x00probe", path)         # datagram dial+send
            finally:
                s.close()
        with pytest.raises(RuntimeError):
            socket.create_connection(path, timeout=0.2)  # type: ignore[arg-type]  # AF_UNIX str address
