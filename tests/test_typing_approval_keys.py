"""Approval answers typed into her window resolve on key RELEASE.

Owner failure (2026-11-09, Hyprland/kitty): approving the Enter ask with a
physical Enter resolved the future at key PRESS; Riko re-focused kitty and
injected virtual keycode 28 milliseconds later while the physical Enter was
still held. The Wayland seat tracks pressed keycodes across all keyboards,
the 28:1 merged into the held key and vanished — kitty came up focused with
Enter already "down" and the shell never saw the tap.

Offscreen Qt only: real QKeyEvents are fed to the handler desktop.py wires
into AvatarWindow.eventFilter. No windows, no compositor, nothing typed."""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _display_var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
    os.environ.pop(_display_var, None)

import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

import desktop


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


class FakeRuntime:
    """Stands in for ClientRuntime's approval surface (the three attributes
    make_approval_key_handler reads)."""

    def __init__(self, pending=None):
        self._pending = pending if pending is not None else \
            "press Enter in terminal kitty \"~\""
        self.answers = []

    def pending_approval(self):
        return self._pending

    def approve_hook(self):
        self.answers.append("approve")
        self._pending = None
        return {"ok": True}

    def deny_hook(self):
        self.answers.append("deny")
        self._pending = None
        return {"ok": True}


def key_event(kind, key, modifiers=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(kind, key, modifiers)


def test_approval_resolves_on_release_not_press(app):
    """The whole bug: answering at press leaves the physical Enter down
    while the engine settles; answering at release cannot."""
    rt = FakeRuntime()
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=False), rt)
    assert handler(key_event(QEvent.Type.KeyPress, Qt.Key.Key_Return)) is True
    assert rt.answers == []                    # swallowed, NOT answered yet
    assert handler(key_event(QEvent.Type.KeyRelease, Qt.Key.Key_Return)) is True
    assert rt.answers == ["approve"]           # answered once the key is up


def test_escape_denies_on_release(app):
    rt = FakeRuntime()
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=False), rt)
    handler(key_event(QEvent.Type.KeyPress, Qt.Key.Key_Escape))
    assert rt.answers == []
    handler(key_event(QEvent.Type.KeyRelease, Qt.Key.Key_Escape))
    assert rt.answers == ["deny"]


def test_numpad_enter_answers_too(app):
    rt = FakeRuntime()
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=False), rt)
    handler(key_event(QEvent.Type.KeyPress, Qt.Key.Key_Enter))
    handler(key_event(QEvent.Type.KeyRelease, Qt.Key.Key_Enter))
    assert rt.answers == ["approve"]


def test_modified_enter_is_not_an_answer(app):
    """Ctrl+Return belongs to the app/page, never to the bubble."""
    rt = FakeRuntime()
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=False), rt)
    mod = Qt.KeyboardModifier.ControlModifier
    assert handler(key_event(QEvent.Type.KeyPress, Qt.Key.Key_Return, mod)) is False
    assert handler(key_event(QEvent.Type.KeyRelease, Qt.Key.Key_Return, mod)) is False
    assert rt.answers == []


def test_nothing_pending_nothing_swallowed(app):
    rt = FakeRuntime(pending="")
    rt._pending = None
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=False), rt)
    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        assert handler(key_event(kind, Qt.Key.Key_Return)) is False
    assert rt.answers == []


def test_open_input_box_owns_the_keys(app):
    """While the chat box has the caret, Enter submits the chat and Escape
    closes the box — the bubble must not steal either key."""
    rt = FakeRuntime()
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=True), rt)
    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        assert handler(key_event(kind, Qt.Key.Key_Return)) is False
    assert rt.answers == []


def test_other_keys_pass_through(app):
    rt = FakeRuntime()
    handler = desktop.make_approval_key_handler(SimpleNamespace(input_open=False), rt)
    assert handler(key_event(QEvent.Type.KeyPress, Qt.Key.Key_W)) is False
    assert handler(key_event(QEvent.Type.KeyRelease, Qt.Key.Key_W)) is False
    assert rt.answers == []
