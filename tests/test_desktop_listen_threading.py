"""The L-key / tray always-listen wiring, headless.

The owner's L-key exit was native (PortAudio closed from a foreign thread —
pinned in tests/test_listen_mode.py). But a Qt slot that dies silently, a
worker poking Qt objects off the GUI thread, or a long mic teardown on the
Qt thread are the OTHER ways one L press could end the app. These pin the
crash-report hooks and the exact signal route the tray relies on.

Qt runs on the offscreen platform with DISPLAY / WAYLAND_DISPLAY cleared,
so this never touches the owner's desktop.
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _display_var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
    os.environ.pop(_display_var, None)

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import desktop  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([sys.argv[0]])


def test_crash_reporting_is_wired():
    """A future native crash must print a backtrace, and a stray Python
    exception (in a slot or a worker) must print and keep the app alive."""
    src = (ROOT / "desktop.py").read_text(encoding="utf-8")
    assert "faulthandler.enable(file=sys.stderr, all_threads=True)" in src
    assert "sys.excepthook = _print_exc" in src
    assert "threading.excepthook = _print_thread_exc" in src
    # the tray tooltip/check update runs in on_command (the Qt thread), fed
    # by the bridge signal — session callbacks never touch Qt objects direct.
    assert 'session.on_listen_mode = lambda mode: bridge.command.emit' in src
    assert 'elif cmd.startswith("listen-ui:")' in src
    # and the mode toggle never blocks on the mic teardown (session.py side)
    sess = (ROOT / "riko" / "session.py").read_text(encoding="utf-8")
    assert 'name="riko-vad-close"' in sess


def test_listen_ui_signal_marshals_to_the_gui_thread(qapp):
    """The exact route a mode change takes: session (ANY thread — L arrives
    on the riko-ctl HTTP thread) -> Bridge.command (QueuedConnection) ->
    on_command on the GUI thread, where the tray action and tooltip live."""
    main_thread = threading.get_ident()
    seen = []
    bridge = desktop.Bridge()
    bridge.command.connect(lambda cmd: seen.append((cmd, threading.get_ident())),
                           Qt.ConnectionType.QueuedConnection)

    def worker():  # the HTTP control thread, or the VAD-close thread
        bridge.command.emit("listen-ui:always")

    t = threading.Thread(target=worker, name="fake-http")
    t.start()
    t.join()
    deadline = time.monotonic() + 3
    while not seen and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert seen, "the bridge never delivered to the GUI thread"
    cmd, ran_on = seen[0]
    assert cmd == "listen-ui:always"
    assert ran_on == main_thread, "the tray update ran off the GUI thread"
