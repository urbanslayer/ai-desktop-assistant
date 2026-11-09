"""Typing/dictation client tools (docs/TYPING.md "Tools"), registered only
while desktop awareness is on.

Registration shape: risk `act` + permission_default `always` means the HUB
does not pre-gate; the executor does the ASK through the existing approval
path by raising client_tools.NeedConfirmation with the summary the owner
sees (target window + text; for Enter in a terminal, the exact command).
That is also what makes the contract's "ALWAYS inside dictation for the
pinned window" work: the runtime check in the executor skips the
confirmation for that one window while dictation is live.

install() returns a TypingService and registers a provider with
client_tools.register_extra(), so the typing pack appears/disappears with
the awareness toggle without desktop_sense importing typing (and lets the
app-editing pack use the same hook).
"""
from __future__ import annotations

import os

from ..client_tools import ToolError
from ..client_tools import NeedConfirmation as HubStyleConfirm
from .dictation import DictationController
from .engine import NeedConfirmation as EngineAsk
from .engine import TypingEngine


def typing_tools():
    obj = {"type": "object", "additionalProperties": False}
    return [
        {"name": "type_text", "risk": "act", "permission_default": "always",
         "description": (
             "Type text at the cursor of the focused window (or the `target` "
             "window, focused first). The owner confirms every type outside "
             "dictation (the approval shows the window and the text); inside "
             "dictation the pinned window needs none. Refused for password "
             "managers, auth prompts and private windows. Terminal text is "
             "single-line and Enter is NEVER included — press_keys('enter') "
             "runs a command, always with approval. Max 4000 chars."),
         "parameters": dict(obj, properties={
             "text": {"type": "string"},
             "target": {"type": "string",
                        "description": "window name/id ref (optional)"}},
             required=["text"])},
        {"name": "press_keys", "risk": "act", "permission_default": "always",
         "description": (
             "Press key combos: 'ctrl+s', 'ctrl+shift+t', 'enter', "
             "'backspace*3' (space/comma separated, max 32). Super/Meta "
             "combos and Ctrl+Alt+Del / Ctrl+Alt+F1-F12 / Ctrl+Alt+Backspace "
             "are refused. In a terminal the run keys (Enter, KP Enter, "
             "Ctrl+M, Ctrl+J, Shift/Alt/Ctrl+Enter) ALWAYS ask with the "
             "exact command line shown first — even in dictation or after a "
             "hub approval — and Ctrl+C/D/Z/\\ interrupt keys ask with the "
             "target window shown."),
         "parameters": dict(obj, properties={
             "keys": {"type": "string"},
             "target": {"type": "string"}}, required=["keys"])},
        {"name": "set_dictation", "risk": "act", "permission_default": "always",
         "description": (
             "Turn dictation mode on/off. While on, her recognised speech is "
             "typed into the window focused at start (the pinned window) "
             "instead of becoming a chat turn. Control phrases: 'stop "
             "dictating', 'new line', 'new paragraph', 'delete that' / "
             "'scratch that', spoken punctuation ('full stop', 'comma', "
             "'question mark'); starting with her persona name sends the "
             "rest to her as a normal chat turn. Typing pauses when focus "
             "leaves the pinned window."),
         "parameters": dict(obj, properties={
             "on": {"type": "boolean"},
             "target": {"type": "string"}}, required=["on"])},
        {"name": "dictation_status", "risk": "read", "permission_default": "always",
         "description": "Dictation mode state: {on, target:{app,title}, paused_reason}.",
         "parameters": dict(obj, properties={})},
        {"name": "stop_typing", "risk": "act", "permission_default": "always",
         "description": ("Abort the keystroke batch in progress right away "
                         "(stops at the next chunk). Does not change "
                         "dictation mode."),
         "parameters": dict(obj, properties={})},
    ]


def typing_executors(engine, dictation):
    def _ask_or_run(tool, fn, **kwargs):
        """Engine-level ASK → the standard confirmation path: the owner is
        asked with the engine's summary, then the runtime re-invokes with
        confirmed=True."""
        try:
            return fn(**kwargs)
        except EngineAsk as c:
            raise HubStyleConfirm(c.reason, tool, kwargs) from None

    def _pinned_now(target):
        """§7: type_text is ALWAYS only for the pinned window itself."""
        if not (dictation.on and dictation.pinned) or dictation.paused_reason:
            return False
        if target not in (None, "") and isinstance(target, str):
            pin = dictation.pinned
            hay = f"{pin.get('app') or ''} {pin.get('title') or ''}".lower()
            if str(target).lower() not in hay:
                return False
        try:
            return engine.same_window(engine.focused(), dictation.pinned)
        except ToolError:
            return False

    def type_text(text=None, target=None, confirmed=False, **_kw):
        return _ask_or_run(
            "type_text", engine.type_text,
            text=text, target=target or None, confirmed=bool(confirmed),
            dictation_pinned=_pinned_now(target))

    def press_keys(keys=None, target=None, confirmed=False, run_token=None,
                   **_kw):
        """Run/interrupt keys into a terminal carry their own one-time
        run_token through the confirmation params: a generic hub approval
        (confirmed=True) or the dictation pin never grants them — the engine
        only accepts the token it issued with the ask that showed the
        command."""
        kwargs = {"keys": keys, "target": target or None,
                  "confirmed": bool(confirmed)}
        if run_token:
            kwargs["run_token"] = str(run_token)
        try:
            return engine.press_keys(**kwargs)
        except EngineAsk as c:
            params = dict(kwargs)
            if getattr(c, "run_token", None):
                params["run_token"] = c.run_token
            raise HubStyleConfirm(c.reason, "press_keys", params) from None

    def set_dictation(on=True, target=None, confirmed=False, **_kw):
        want = bool(on)
        if want == dictation.on and not target:
            return dictation.status()
        if not confirmed:
            where = ""
            if want:
                try:
                    w = engine.resolve_target(target or None)
                    where = f" into {w.get('app')} \"{w.get('title') or ''}\""
                except ToolError:
                    where = " (no window resolves yet)"
            raise HubStyleConfirm(("start dictation" + where) if want
                                  else "stop dictation",
                                  "set_dictation",
                                  {"on": want, "target": target})
        return (dictation.start(target=target or None) if want
                else dictation.stop())

    def dictation_status(**_kw):
        return dictation.status()

    def stop_typing(**_kw):
        return {"stopped": True, "note": engine.stop()}

    return {
        "type_text": type_text,
        "press_keys": press_keys,
        "set_dictation": set_dictation,
        "dictation_status": dictation_status,
        "stop_typing": stop_typing,
    }


class TypingService:
    """Everything desktop.py needs: the engine, the dictation controller,
    the session speech-router and the tools provider."""

    def __init__(self, engine, dictation, provider):
        self.engine = engine
        self.dictation = dictation
        self.provider = provider
        self.route = dictation.route


def install(sense, *, cfg=None, bridge=None, runtime=None, runner=None,
            env=None, on_notice=None, ask=None, printer=print):
    """Build the typing service for this desktop and register it with
    client_tools so the tools ride the awareness gate. Call AFTER
    start_desktop (it needs the DesktopSense). `on_notice(text)` shows the
    dictation pill/notice; `ask(summary, timeout)` is the approval bubble
    (defaults to runtime.ask_approval)."""
    from .. import client_tools

    d = (cfg or {}).get("desktop") or {}
    layout = str(d.get("keyboard_layout") or "").strip() or None
    engine = TypingEngine(sense, runner=runner,
                          env=env if env is not None else os.environ,
                          layout=layout, printer=printer)

    def _persona_names():
        try:
            from .. import hub_client
            return list(hub_client.persona_names() or [])
        except Exception:                        # noqa: BLE001
            return []

    _notice = on_notice or (lambda msg: print(f"[dictation] {msg}", flush=True))
    _ask = ask or (runtime.ask_approval if runtime is not None else None)
    dictation = DictationController(engine, notice=_notice,
                                    persona_names=_persona_names,
                                    ask=_ask, printer=printer)

    def provider(sense=None):
        return typing_tools(), typing_executors(engine, dictation)

    client_tools.register_extra(provider)
    if runtime is not None and getattr(sense, "awareness", None) is not None \
            and sense.awareness.enabled:
        runtime.set_tools(client_tools.desktop_tools(sense),
                          client_tools.make_executors(bridge, sense,
                                                      runtime.awareness_toggle))
    return TypingService(engine, dictation, provider)
