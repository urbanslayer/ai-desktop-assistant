"""Typing tools (docs/TYPING.md) — fixture-driven, fake-runner tests. Nothing
here sends real keystrokes: ydotool and the clipboard go through fake runners,
the window manager is a FakeSense fed by plain dicts, and the conftest guard
hard-fails any test that still tries to exec ydotool/ydotoold/wl-copy/wl-paste
or reach the Obsidian REST ports."""
import json
import socket
import subprocess
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

    def executors(self):
        return {}


class FakeYD:
    """Stands in for YdotoolBackend: records every batch, can act mid-send."""

    def __init__(self, on_send=None):
        self.batches = []
        self.on_send = on_send

    def require(self):
        return None

    def send_events(self, events):
        self.batches.append(list(events))
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


def make_engine(window=None, hide=(), layout="gb", on_send=None, lines=None):
    sense = FakeSense(window, hide)
    yd = FakeYD(on_send)
    eng = TypingEngine(sense, yd=yd, clipboard=FakeClip(), layout=layout,
                       printer=(lines.append if lines is not None
                                else (lambda m: None)))
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


def test_terminal_enter_always_asks_with_the_command():
    eng, _, yd = make_engine(window=KITTY)
    eng.type_text("rm -rf / --no-preserve-root", confirmed=True)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    assert "rm -rf / --no-preserve-root" in e.value.reason
    assert len(yd.batches) == 1                           # only the typing so far
    out = eng.press_keys("enter", confirmed=True)         # approved: now it runs
    assert out["pressed"] == 1
    assert (28, 1) in yd.batches[-1]
    # after Enter the remembered command line is gone (it ran)
    eng.press_keys("l", confirmed=True)
    with pytest.raises(NeedConfirmation) as e2:
        eng.press_keys("enter")
    assert "l" in e2.value.reason


def test_non_terminal_enter_needs_no_command_summary():
    eng, _, yd = make_engine(window=KATE)
    with pytest.raises(NeedConfirmation) as e:
        eng.press_keys("enter")
    assert "press enter" in e.value.reason.lower()
    eng.press_keys("enter", confirmed=True)
    assert (28, 1) in yd.batches[-1]


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
