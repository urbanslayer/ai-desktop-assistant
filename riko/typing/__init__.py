"""Desktop typing + dictation (docs/TYPING.md).

ydotool is the ONLY keystroke backend and it is never invoked for real in
tests: every runner is fake, every key goes through the layout table, and
the test guard (tests/conftest.py) hard-fails any attempt to exec
ydotool/ydotoold/wl-copy/wl-paste or connect the real daemon socket.
"""
from .dictation import DictationController
from .engine import NeedConfirmation, TypingEngine
from .layout import detect_layout, resolve_char
from .terminals import is_terminal
from .tools import install, typing_executors, typing_tools
from .ydotool import OWNER_SETUP, YdotoolBackend

__all__ = [
    "DictationController", "TypingEngine", "NeedConfirmation",
    "OWNER_SETUP", "YdotoolBackend", "detect_layout", "install",
    "is_terminal", "resolve_char", "typing_executors", "typing_tools",
]
