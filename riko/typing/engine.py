"""TypingEngine (docs/TYPING.md §1, §3, §5, §6): the gate in front of every
keystroke.

Order of operations for any call: validate → resolve + check the target
window (denylist, private, target mismatch) → approval gate (dictation-
pinned window excepted) → backend availability → batch under a lock,
re-checking the target between chunks and honouring abort. The audit line
carries counts and window names, never the typed text.

Terminal run/interrupt keys (§6) are the exception to the approval gate:
NEITHER a generic hub approval (`confirmed=True`) NOR the dictation pin
satisfies them. Only `run_token` does — a one-time token this engine issues
with its own NeedConfirmation, bound to the window id + the exact pending
command line + a short expiry, and echoed back by the confirmation re-call.
That is what guarantees the owner always SEES the command before Enter runs
it."""
from __future__ import annotations

import secrets
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
RUN_TOKEN_TTL = 120.0      # s: an approval bubble that stale is worthless

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
    `reason` is the approval summary shown to her. `run_token` is set for
    terminal run/interrupt asks: the executor must echo it back on the
    confirmed re-call (see TypingEngine._consume_run_token)."""

    def __init__(self, reason, run_token=None):
        super().__init__(reason)
        self.reason = reason
        self.run_token = run_token


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
        self._run_tokens: dict[str, dict] = {}   # run_token → grant record (§6 gate)

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

    # ── terminal run-token gate (§6) ──────────────────────────
    def _issue_run_token(self, w: dict, cmd: str, kind: str) -> str:
        """One-time token granting Enter/interrupt into this terminal window.
        Bound to (window id, exact command text, kind, expiry)."""
        tok = secrets.token_urlsafe(16)
        now = time.monotonic()
        # stale approvals are worthless; don't let the store grow
        self._run_tokens = {t: r for t, r in self._run_tokens.items()
                            if r["expires"] > now}
        self._run_tokens[tok] = {
            "win": str(w.get("id") or ""),
            "cmd": cmd,
            "kind": kind,
            "expires": now + RUN_TOKEN_TTL,
        }
        return tok

    def _consume_run_token(self, token, w: dict, kind: str) -> bool:
        """Validate and burn a run token. False = no grant (expired, wrong
        window, command changed, wrong kind, already spent)."""
        if not token:
            return False
        rec = self._run_tokens.pop(str(token), None)
        if rec is None:
            return False
        if time.monotonic() > rec["expires"]:
            return False
        if rec["win"] != str(w.get("id") or ""):
            return False
        if rec["kind"] != kind:
            return False
        # command must still be exactly what the owner saw in the bubble
        cur = (self._last_line.get(str(w.get("id"))) or "").strip()
        return cur == rec["cmd"]

    def _summary_run(self, w, cmd, kind):
        where = f"{w.get('app')} \"{w.get('title') or ''}\""
        cmd_disp = (f"command: `{_preview(cmd, 200)}`" if cmd
                    else "unknown command — Riko did not type this line")
        if kind == "interrupt":
            return (f"send Ctrl+C (interrupt) to terminal {where} — "
                    f"{cmd_disp}")
        return f"press Enter in terminal {where} — {cmd_disp}"

    # ── press_keys ─────────────────────────────────────────────
    def press_keys(self, keys, target=None, confirmed=False,
                   dictation_pinned=False, run_token=None) -> dict:
        combos = keys_mod.parse_keys(keys)
        w = self.resolve_target(target)
        self._reject(w)
        terminal = is_terminal(str(w.get("app") or ""), str(w.get("title") or ""))
        # Classify every combo: the engine tracks run/interrupt state per
        # key, not a single bool, so the send loop can clear _last_line and
        # burn the token at the right moment.
        kinds = [keys_mod.terminal_key_kind(m, b, code)
                 for (m, b, code, _c) in combos]
        needs_run_gate = terminal and any(kinds)
        # §6: terminal run/interrupt keys need a one-time run_token issued by
        # the engine's own NeedConfirmation round trip.  A generic
        # `confirmed=True` from the hub bubble (which shows only the tool
        # args, not the command) and the dictation pin can NEVER substitute.
        if needs_run_gate:
            cmd = (self._last_line.get(str(w.get("id"))) or "").strip()
            first_kind = next(k for k in kinds if k)   # 'run' or 'interrupt'
            if not self._consume_run_token(run_token, w, first_kind):
                raise NeedConfirmation(self._summary_run(w, cmd, first_kind),
                                       run_token=self._issue_run_token(
                                           w, cmd, first_kind))
        elif not (confirmed or dictation_pinned):
            where = f"{w.get('app')} \"{w.get('title') or ''}\""
            raise NeedConfirmation(f"press {_preview(str(keys), 60)} in {where}")
        granted = bool(run_token and needs_run_gate)
        with self._batch_guard():
            self._recheck(w)
            t0 = time.monotonic()
            sent = 0
            for (mods, base, code, count), kind in zip(combos, kinds):
                for _ in range(count):
                    if self.abort.is_set():
                        raise ToolError("typing aborted before that key")
                    self._send(keys_mod.combo_events(mods, code))
                    sent += 1
                if terminal and kind:
                    # run executed the line, interrupt discarded it — either
                    # way the shell prompt is fresh
                    self._last_line.pop(str(w.get("id")), None)
                elif terminal:
                    self._track_key(w, mods, base, count)
        sec = (time.monotonic() - t0)
        self.printer(f'[typing] keys "{_preview(str(keys), 60)}" → '
                     f'{w.get("app")}'
                     + (" (dictation)" if dictation_pinned else "")
                     + (" (run-token)" if granted else "")
                     + f" {sec:.1f}s")
        return {"pressed": sent, "target": _win_brief(w)}

    def _track_key(self, w: dict, mods, base, count) -> None:
        """Update _last_line for a non-run, non-interrupt key sent to a
        terminal so the next Enter summary reflects what was actually typed."""
        wid = str(w.get("id") or "")
        if base in ("backspace", "del") and not mods:
            cur = self._last_line.get(wid, "")
            self._last_line[wid] = cur[:-count] if count <= len(cur) else ""
        elif not mods and (base in keys_mod.BASE_CHARS or base == "space"):
            # a printable char key (a-z, 0-9, space, symbols)
            ch = " " if base == "space" else base
            self._last_line[wid] = (self._last_line.get(wid, "")
                                    + ch * count)[-400:]

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
