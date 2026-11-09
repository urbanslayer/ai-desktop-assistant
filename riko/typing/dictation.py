"""Dictation mode (docs/TYPING.md §8): recognised speech goes to the pinned
window as keystrokes instead of a hub chat turn.

- Control phrases are exact-utterance matches (never mid-sentence): stop
  dictating / new line / new paragraph / delete that + spoken punctuation.
- A leading vocative ("Riko, …" using the active persona's name) leaves
  dictation ON and hands the rest to the hub as a normal chat turn.
- Focus leaving the pinned window PAUSES typing (a local notice, no hub
  turn); it resumes silently when focus returns.
- While ON, the pinned window has type_text ALWAYS (§7) — enforced here
  (the executor passes dictation_pinned=True only for this window).
"""
from __future__ import annotations

import re

from ..client_tools import ToolError
from .engine import _preview
from .terminals import is_terminal

_STOP = {"stop dictating", "stop dictation", "end dictation",
         "quit dictating", "quit dictation", "stop listening to me"}
_NEWLINE = {"new line", "newline", "next line"}
_PARAGRAPH = {"new paragraph", "new paragraph please"}
_DELETE = {"delete that", "scratch that", "undo that", "forget that"}
_PUNCT = {
    "full stop": ". ", "period": ". ",
    "comma": ", ", "question mark": "? ", "comma please": ", ",
    "full stop please": ". ", "question mark please": "? ",
    "period please": ". ",
}

_norm_re = re.compile(r"[.!?\u3002\uff1f\uff01]+$")


def _norm(text: str) -> str:
    return _norm_re.sub("", str(text or "").strip().lower()).strip()


class DictationController:
    """State machine + speech router. `engine` is a TypingEngine;
    `notice(text)` shows local UI (pill/tray), `persona_names()` returns the
    vocative names of the ACTIVE persona (hub data), `ask(summary)` runs the
    approval bubble for Enter-in-terminal while dictating."""

    def __init__(self, engine, *, notice=None, persona_names=None,
                 ask=None, printer=print):
        self.engine = engine
        self.notice = notice or (lambda msg: None)
        self.persona_names = persona_names or (lambda: [])
        self.ask = ask
        self.printer = printer
        self.on = False
        self.pinned: dict | None = None      # {"id","app","title"}
        self.paused_reason = ""
        self._last_chunk = ""
        self._paused_announced = False

    # ── lifecycle ──────────────────────────────────────────────
    def start(self, target=None) -> dict:
        w = self.engine.resolve_target(target)
        self.engine._reject(w)               # denylist/private can't be pinned
        if str(w.get("app") or "") in ("", "?"):
            raise ToolError("dictation: I cannot tell which window is focused")
        self.on = True
        self.pinned = w
        self.paused_reason = ""
        self._last_chunk = ""
        self._paused_announced = False
        self.notice(f"dictating into {w.get('app')}")
        return {"dictation": True, "target": {"id": str(w.get("id")),
                                              "app": w.get("app"),
                                              "title": str(w.get("title") or "")[:120]}}

    def stop(self) -> dict:
        was = self.on
        self.on = False
        self.pinned = None
        self.paused_reason = ""
        if was:
            self.notice("dictation off")
        return {"dictation": False, "was_on": was}

    def status(self) -> dict:
        return {"on": self.on,
                "paused": bool(self.paused_reason),
                "paused_reason": self.paused_reason,
                "target": ({"app": self.pinned.get("app"),
                            "title": str(self.pinned.get("title") or "")[:120]}
                           if self.pinned else None)}

    # ── focus gate (§8 pause/resume) ───────────────────────────
    def _focus_ok(self) -> bool:
        try:
            w = self.engine.focused()
        except ToolError as exc:
            return self._pause(str(exc))
        if not self.engine.same_window(self.pinned, w):
            return self._pause(f"{w.get('app') or 'another window'} has focus")
        if self.paused_reason:
            self.paused_reason = ""
            self._paused_announced = False
            self.notice("dictation resumed")   # resume: quiet, one line
        return True

    def _pause(self, reason) -> bool:
        self.paused_reason = reason
        if not self._paused_announced:
            self._paused_announced = True
            self.notice(f"dictation paused — {reason}")   # local notice only
        return False

    # ── the speech router ──────────────────────────────────────
    def route(self, text: str):
        """Returns (handled, replacement). handled=True → NO hub turn.
        handled=False → normal chat turn with `replacement` as the text."""
        if not self.on:
            return False, None
        raw = str(text or "").strip()
        if not raw:
            return False, None
        # vocative first: "Riko, sort my tabs" is a CHAT TURN, dictation stays on
        rest = self._strip_vocative(raw)
        if rest is not None:
            if not rest:
                return True, None             # she only said her name
            return False, rest
        if not self._focus_ok():
            return True, None                 # paused: speech neither types nor chats
        phrase = _norm(raw)
        try:
            if phrase in _STOP:
                self.stop()
                return True, None
            if phrase in _NEWLINE:
                self._send_line_break(1)
                return True, None
            if phrase in _PARAGRAPH:
                self._send_line_break(2)
                return True, None
            if phrase in _DELETE:
                self._delete_last()
                return True, None
            if phrase in _PUNCT:
                self._type(_PUNCT[phrase], bare=True)
                return True, None
        except ToolError as exc:
            self.notice(f"dictation: {exc}")
            return True, None
        self._type(raw)
        return True, None

    def _strip_vocative(self, text):
        """Return the remainder when the utterance opens with the active
        persona's name (", " optional in speech), else None."""
        low = text.lower()
        for name in self.persona_names():
            n = str(name or "").strip().lower()
            if not n:
                continue
            if low.startswith(n):
                rest = text[len(n):].lstrip(" ,.，、")
                if rest or low == n:
                    return rest
        return None

    # ── typing helpers (pinned window only — ALWAYS per §7) ────
    def _type(self, text, bare=False):
        chunk = text if (bare or not self._last_chunk) else " " + text
        self.engine.type_text(chunk, target=self.pinned,
                              dictation_pinned=True)
        if not bare:
            self._last_chunk = (self._last_chunk + chunk).strip()

    def _send_line_break(self, n):
        w = self.engine.focused()
        if is_terminal(str(w.get("app") or ""), str(w.get("title") or "")):
            # Enter in a terminal ALWAYS asks, even mid-dictation (§6), and
            # the ask must show the exact command line. With no approval
            # bubble wired there is no way to ask — so no Enter: a silent
            # unasked Enter into a shell is exactly what §6 forbids.
            cmd = (self.engine._last_line.get(str(w.get("id"))) or "").strip()
            if self.ask is None:
                self.notice("dictation: no approval bubble wired — "
                            "Enter not sent to the terminal")
                self._last_chunk = ""
                return
            summary = (f"press Enter in terminal {w.get('app')} "
                       f"\"{w.get('title') or ''}\" — dictation line break — "
                       + (f"command: `{_preview(cmd, 200)}`" if cmd
                          else "unknown command — Riko did not type this line"))
            if bool(self.ask(summary, 120)):
                self.engine._send_combo("enter")
                self.engine._last_line.pop(str(w.get("id")), None)
            else:
                self.notice("dictation: line break declined")
        else:
            self.engine.type_text("\n" * n, target=self.pinned,
                                  dictation_pinned=True)
        self._last_chunk = ""

    def _delete_last(self):
        n = len(self._last_chunk)
        self._last_chunk = ""
        if not n:
            self.notice("dictation: nothing to delete")
            return
        remaining = n
        while remaining > 0:
            step = min(remaining, 64)
            self.engine.press_keys(f"backspace*{step}", target=self.pinned,
                                   dictation_pinned=True)
            remaining -= step
