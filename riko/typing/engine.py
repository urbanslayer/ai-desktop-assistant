"""TypingEngine (docs/TYPING.md §1, §3, §5, §6): the gate in front of every
keystroke.

Order of operations for any call: validate → resolve + check the target
window (denylist, private, target mismatch) → approval gate (dictation-
pinned window excepted) → backend availability → batch under a lock,
re-checking the target between chunks and honouring abort. The audit line
carries counts and window names, never the typed text."""
from __future__ import annotations

import threading
import time

from ..client_tools import ToolError
from . import keys as keys_mod
from .clipboard import Clipboard, ClipboardUnavailable, paste_text
from .keycodes import NAMED_KEYS
from .layout import detect_layout, resolve_char
from .terminals import is_terminal
from .ydotool import YdotoolBackend, YdotoolUnavailable

MAX_CHARS = 4000
CHUNK_CHARS = 64           # abort/target granularity while a batch runs
_KEYS_MAX_CHARS = 200      # beyond this, paste regardless of mapping

# §2: auth prompts and lock screens are typing targets too dangerous to
# name, beyond what the DesktopSense denylist covers by default.
_AUTH_DENY = ("pinentry", "polkit", "policykit", "gkr-pam", "xdg-desktop-portal",
              "portal", "sddm", "login", "light-locker", "gsd-screensaver",
              "kscreenlocker", "hyprlock", "swaylock")


def _auth_agent(app: str, title: str) -> bool:
    hay = f"{app} {title}".lower()
    return any(h in hay for h in _AUTH_DENY)


class NeedConfirmation(Exception):
    """Raised instead of sending: the executor wants the owner's OK first.
    `reason` is the approval summary shown to her."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _preview(text: str, n: int = 120) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n] + "…"


class TypingEngine:
    def __init__(self, sense, *, yd=None, clipboard=None, runner=None,
                 env=None, layout=None, max_chars=MAX_CHARS, printer=print):
        self.sense = sense
        self.runner = runner
        self.yd = yd or YdotoolBackend(runner=runner, env=env)
        self.clip = clipboard or Clipboard(runner=runner, env=env)
        self._layout = layout                    # explicit wins (§4)
        self._layout_src = "configured" if layout else None
        self.max_chars = int(max_chars)
        self.printer = printer
        self._lock = threading.Lock()
        self.abort = threading.Event()
        self._last_line: dict[str, str] = {}     # window id → typed, for Enter summary

    # ── layout ─────────────────────────────────────────────────
    @property
    def layout(self):
        if self._layout is None:
            backend = getattr(self.sense, "backend", None)
            kind = str(getattr(backend, "name", "") or "")
            files = getattr(getattr(backend, "lua", None), "files_read", None)
            self._layout, src = detect_layout(
                backend_kind=kind,
                runner=getattr(backend, "runner", None) or self.runner,
                hypr_files=files, log=self.printer)
            self._layout_src = src
            self.printer(f'[typing] keyboard layout: {self._layout} ({src})')
        return self._layout

    # ── window checks (§3 target check, §5 denylist) ───────────
    def focused(self) -> dict:
        b = getattr(self.sense, "backend", None)
        if b is None:
            raise ToolError("desktop awareness is off — I can't see a window "
                            "to type into")
        try:
            w = b.focused()
        except ToolError:
            raise
        except Exception as exc:                 # noqa: BLE001
            raise ToolError(f"cannot read the focused window: {exc}") from None
        if not w:
            raise ToolError("no focused window to type into")
        return w

    def _reject(self, w: dict):
        aw = getattr(self.sense, "awareness", None)
        app, title = str(w.get("app") or ""), str(w.get("title") or "")
        if aw is not None:
            if aw.hides(app, title):
                raise ToolError(f"{app or title or 'that window'} is on the "
                                "desktop denylist — I won't type into it")
            if aw.hides_private(title):
                raise ToolError("that is a private browsing window — I won't "
                                "type into it")
        if _auth_agent(app, title):
            raise ToolError(f"{app or title} is an auth prompt or lock "
                            "screen — I won't type into it")

    def resolve_target(self, target=None) -> dict:
        if isinstance(target, dict):             # a pinned dictation target
            w = self.focused()
            if str(w.get("id")) != str(target.get("id")):
                raise ToolError(
                    f"the pinned window is not focused ({w.get('app') or '?'} "
                    "has focus) — nothing was typed")
            return w
        if target:
            self.sense.focus_window(str(target))
        return self.focused()

    @staticmethod
    def same_window(a, b) -> bool:
        try:
            return bool(a) and bool(b) and str(a.get("id")) == str(b.get("id"))
        except Exception:                        # noqa: BLE001
            return False

    # ── sending ────────────────────────────────────────────────
    def _send(self, events):
        try:
            self.yd.send_events(events)
        except YdotoolUnavailable as exc:
            raise ToolError(str(exc)) from None

    def _send_combo(self, combo):
        mods, base, code, count = keys_mod.parse_combo(combo)
        ev = keys_mod.combo_events(mods, code)
        for _ in range(count):
            self._send(ev)

    # ── type_text ──────────────────────────────────────────────
    def type_text(self, text, target=None, confirmed=False,
                  dictation_pinned=False) -> dict:
        text = str(text or "")
        if not text.strip() and not any(c in "\n\t" for c in text):
            raise ToolError("type_text: nothing to type")
        if len(text) > self.max_chars:
            raise ToolError(f"type_text: {len(text)} characters is over the "
                            f"{self.max_chars}-character cap — send it in parts")
        w = self.resolve_target(target)
        self._reject(w)
        terminal = is_terminal(str(w.get("app") or ""), str(w.get("title") or ""))
        if terminal:
            text = text.replace("\r", "").replace("\n", "")
            if any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 for c in text):
                raise ToolError("terminal text contains control characters — "
                                "I refuse to feed escape sequences to a shell")
        else:
            cleaned = "".join(c for c in text if ord(c) >= 32 or c in "\n\t")
            if cleaned != text:
                raise ToolError("text contains control characters I cannot "
                                "type (escape sequences are refused)")
        if not (confirmed or dictation_pinned):
            raise NeedConfirmation(self._summary_text(w, text, terminal))
        return self._batch(text, w, terminal, mode_hint=None,
                           dictation=bool(dictation_pinned))

    def _summary_text(self, w, text, terminal):
        where = f"{w.get('app') or '?'} \"{w.get('title') or ''}\""
        tail = " — Enter NOT pressed" if terminal else ""
        return (f'type {len(text)} chars into {where}{tail}: '
                f'"{_preview(text)}"')

    # ── press_keys ─────────────────────────────────────────────
    def press_keys(self, keys, target=None, confirmed=False,
                   dictation_pinned=False) -> dict:
        combos = keys_mod.parse_keys(keys)
        w = self.resolve_target(target)
        self._reject(w)
        terminal = is_terminal(str(w.get("app") or ""), str(w.get("title") or ""))
        has_enter = any(code in keys_mod.ENTER_CODES
                        for (m, b, code, c) in combos
                        if keys_mod.is_plain_enter(m, b))
        # press_keys is act/ASK overall; Enter-into-a-terminal is the case
        # that shows the exact command and can never skip the ask.
        if not (confirmed or dictation_pinned):
            where = f"{w.get('app')} \"{w.get('title') or ''}\""
            if terminal and has_enter:
                raise NeedConfirmation(self._summary_enter(w))
            raise NeedConfirmation(f"press {_preview(str(keys), 60)} in {where}")
        with self._batch_guard():
            self._recheck(w)
            t0 = time.monotonic()
            sent = 0
            for (mods, base, code, count) in combos:
                for _ in range(count):
                    if self.abort.is_set():
                        raise ToolError("typing aborted before that key")
                    self._send(keys_mod.combo_events(mods, code))
                    sent += 1
                if code in keys_mod.ENTER_CODES and terminal:
                    self._last_line.pop(str(w.get("id")), None)
        sec = (time.monotonic() - t0)
        self.printer(f'[typing] keys "{_preview(str(keys), 60)}" → '
                     f'{w.get("app")}'
                     + (" (dictation)" if dictation_pinned else "")
                     + f" {sec:.1f}s")
        return {"pressed": sent, "target": _win_brief(w)}

    def _summary_enter(self, w):
        cmd = (self._last_line.get(str(w.get("id"))) or "").strip()
        where = f"{w.get('app')} \"{w.get('title') or ''}\""
        if cmd:
            return (f"press Enter in terminal {where} — command: `{_preview(cmd, 200)}`")
        return (f"press Enter in terminal {where} — Riko cannot read the "
                "screen, so CHECK the command line before approving")

    # ── the batch machinery ────────────────────────────────────
    def _batch_guard(self):
        engine = self

        class _Guard:
            def __enter__(self):
                if not engine._lock.acquire(timeout=2):
                    raise ToolError("another keystroke batch is still running "
                                    "— stop_typing cancels it")
                engine.abort.clear()
                return engine

            def __exit__(self, *exc):
                engine._lock.release()
                return False
        return _Guard()

    def _recheck(self, expected: dict):
        """§3: re-read the focused window before each batch/chunk."""
        now = self.focused()
        if not self.same_window(expected, now):
            raise ToolError(
                f"the focused window changed (now {now.get('app') or '?'} "
                f"\"{now.get('title') or ''}\") — typing stopped")
        self._reject(now)
        return now

    def _batch(self, text: str, w: dict, terminal: bool, mode_hint,
               dictation: bool = False) -> dict:
        layout = self.layout
        if mode_hint is None:
            mode_hint = "keys" if (len(text) <= _KEYS_MAX_CHARS
                                   and self._all_mappable(text, layout)) else "paste"
        with self._batch_guard():
            try:
                self.yd.require()                # fail before anything moves
            except YdotoolUnavailable as exc:
                raise ToolError(str(exc)) from None
            self._recheck(w)
            t0 = time.monotonic()
            if mode_hint == "paste":
                self._do_paste(text, w, terminal)
                typed = len(text)
            else:
                typed = self._do_keys(text, w, layout)
        sec = (time.monotonic() - t0)
        self._last_line[str(w.get("id"))] = (self._last_line.get(str(w.get("id")), "")
                                             + text)[-400:]
        # audit line (contract format) — counts and window names only,
        # never the text
        self.printer(f'[typing] {typed} chars → {w.get("app")} '
                     f'"{w.get("title") or ""}"'
                     + (" (dictation)" if dictation else "")
                     + f" {sec:.1f}s (layout {layout}, {mode_hint})")
        return {"typed": typed, "method": mode_hint, "target": _win_brief(w)}

    def _all_mappable(self, text, layout):
        return all(resolve_char(layout, c) is not None
                   for c in text if c not in "\n\t")

    def _do_keys(self, text, w, layout):
        typed = 0
        for i in range(0, len(text), CHUNK_CHARS):
            if self.abort.is_set():
                raise ToolError(f"typing aborted after {typed} chars")
            if i:
                self._recheck(w)
            chunk = text[i:i + CHUNK_CHARS]
            events = []
            for c in chunk:
                if c == "\n":
                    events += [(NAMED_KEYS["enter"], True), (NAMED_KEYS["enter"], False)]
                    continue
                if c == "\t":
                    events += [(NAMED_KEYS["tab"], True), (NAMED_KEYS["tab"], False)]
                    continue
                hit = resolve_char(layout, c)
                if hit is None:                  # raced table: paste instead
                    self._do_paste(chunk, w, is_terminal(str(w.get("app") or "")))
                    return typed + len(chunk)
                code, mods = hit
                for m in mods:
                    events.append((keys_mod.MODIFIERS[m], True))
                events.append((code, True))
                events.append((code, False))
                for m in reversed(mods):
                    events.append((keys_mod.MODIFIERS[m], False))
            self._send(events)
            typed += len(chunk)
        return typed

    def _do_paste(self, text, w, terminal):
        try:
            paste_text(text, terminal, self.clip, self._send_combo)
        except ClipboardUnavailable as exc:
            raise ToolError(f"the paste fallback failed: {exc}") from None

    # ── abort ──────────────────────────────────────────────────
    def stop(self) -> str:
        self.abort.set()
        return "typing stop requested — the current batch stops at the next chunk"


def _win_brief(w):
    return {"app": str(w.get("app") or ""), "title": str(w.get("title") or "")[:120]}
