"""Clipboard paste fallback (docs/TYPING.md §3, §4): Wayland first
(wl-clipboard, which the viewer already ships with), X11 via xclip/xsel.

The clipboard is hers: save it before, restore it after, always. Paste is
Ctrl+V — Ctrl+Shift+V inside terminals, single line only there."""
import os
import shutil

from ..desktop_sense.util import run as _util_run

_TOOLS = {
    "wayland": {"read": ["wl-paste", "-n"],
                "write": ["wl-copy", "--foreground"],
                "clear": ["wl-copy", "--clear"]},
    "x11": {"read": ["xclip", "-selection", "clipboard", "-o"],
            "write": ["xclip", "-selection", "clipboard"],
            "clear": ["xclip", "-selection", "clipboard"]},
    "x11sel": {"read": ["xsel", "-b", "-o"],
               "write": ["xsel", "-b", "-i"],
               "clear": ["xsel", "-b", "-i"]},
}


class ClipboardUnavailable(RuntimeError):
    pass


class Clipboard:
    """Text clipboard through an injectable runner. `env` decides
    wl-* vs x11 tools the same way the rest of Riko does (WAYLAND_DISPLAY
    wins). Tests pass fake runners; nothing here ever runs for real there."""

    def __init__(self, runner=None, env=None):
        self.runner = runner or _util_run
        self.env = dict(env if env is not None else os.environ)

    def tool(self):
        for name in (("wayland", "x11", "x11sel")
                     if self.env.get("WAYLAND_DISPLAY")
                     else ("x11", "x11sel", "wayland")):
            probe = _TOOLS[name]["write"][0]
            if shutil.which(probe, path=self.env.get("PATH")):
                return name
        return None

    def read(self):
        name = self.tool()
        if not name:
            raise ClipboardUnavailable(
                "no clipboard tool found (install wl-clipboard or xclip)")
        argv = _TOOLS[name]["read"]
        done = self.runner(argv, timeout=5, env=self.env)
        if getattr(done, "returncode", 1) != 0:
            return ""     # empty/unreadable clipboard: restoring "" is fine
        return done.stdout or ""

    def write(self, text):
        name = self.tool()
        if not name:
            raise ClipboardUnavailable(
                "no clipboard tool found (install wl-clipboard or xclip)")
        argv = _TOOLS[name]["write"]
        done = self.runner(argv, timeout=5, env=self.env, input=text)
        if getattr(done, "returncode", 1) != 0:
            raise ClipboardUnavailable(f"{' '.join(argv)} failed to write "
                                       "the clipboard")

    def clear(self):
        name = self.tool()
        if not name:
            return
        self.runner(_TOOLS[name]["clear"], timeout=5, env=self.env,
                    input="" if name != "wayland" else None)


def paste_text(text, terminal, clipboard, send_keys):
    """Save → write → Ctrl(+Shift)+V → restore. `send_keys(combo)` sends one
    key combo through the ydotool backend. The clipboard is restored even
    when the paste itself raises."""
    if clipboard.tool() is None:
        raise ClipboardUnavailable(
            "the paste fallback needs a clipboard tool (wl-clipboard or xclip)")
    saved = clipboard.read()
    try:
        clipboard.write(text)
        send_keys("ctrl+shift+v" if terminal else "ctrl+v")
    finally:
        try:
            if saved:
                clipboard.write(saved)
            else:
                clipboard.clear()
        except ClipboardUnavailable:
            pass   # her clipboard is worse off, but we never lose the text
