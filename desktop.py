"""Riko on your desktop: a borderless, see-through window where she just hovers.

    uv run desktop.py

In the window:
    Enter or /      type to her (Enter sends, Esc closes)
    Space           start / stop talking (push-to-talk, your local mic)
    drag            move the window          right-drag  turn her around
    shift-drag      move her inside it       scroll      zoom
    Ctrl+Q          quit (also in the tray menu)

From anywhere (bind these to keys in your compositor, see README):
    bin/riko-ctl type | talk | talk-start | talk-stop | toggle | forget | quit
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from riko import asr, llm, tts, viewer
from riko.config import load_config
from riko.session import Session

APP_ID = "riko"  # Wayland app_id / X11 class; window rules match on this


class Bridge(QObject):
    """Carries commands from the HTTP thread (riko-ctl, the page) onto the Qt thread."""
    command = Signal(str)


class AvatarPage(QWebEnginePage):
    """Repeats the page's console in our terminal, so a broken script here is visible there."""

    NAMES = {"InfoMessageLevel": "log", "WarningMessageLevel": "warning", "ErrorMessageLevel": "error"}

    def javaScriptConsoleMessage(self, level, message, line, source):
        name = self.NAMES.get(getattr(level, "name", ""), "log")
        print(f"[js:{name}] {message}  ({source}:{line})", flush=True)


class AvatarWindow(QWebEngineView):
    def __init__(self, cfg):
        super().__init__()
        # setPage() rather than the QWebEngineView(page) constructor: through the
        # constructor PySide6 keeps the view's own default page and our console
        # forwarding below never runs.
        self.setPage(AvatarPage(self))
        d = cfg.get("desktop") or {}
        self.input_open = False
        self._drag_target = None

        self.setWindowTitle("Riko")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Without this the render widget gets its own native window, which breaks
        # the translucent background and leaves a black strip on resized Wayland windows.
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.resize(int(d.get("width", 420)), int(d.get("height", 680)))

        self.page().setBackgroundColor(Qt.GlobalColor.transparent)
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)  # she can talk right away
        s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.loadFinished.connect(self._hook_mouse)
        self.load(QUrl(f"http://{viewer.HOST}:{viewer.PORT}/?desktop=1"))

        QShortcut(QKeySequence("Ctrl+Q"), self, QApplication.quit)

    # Chromium draws into a child widget; catch left-drags there and hand them to the
    # compositor as a window move (works on Wayland, where apps can't place their own windows).
    def _hook_mouse(self, ok):
        target = self.focusProxy()
        if target is not None and target is not self._drag_target:
            target.installEventFilter(self)
            self._drag_target = target

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and not (self.input_open and event.position().y() > self.height() - 80)):
            self.windowHandle().startSystemMove()
            return True
        return super().eventFilter(obj, event)

    def js(self, code):
        self.page().runJavaScript(code)


def _tray_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#b07cf0"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 60, 60)
    p.setPen(QColor("white"))
    p.setFont(QFont("Sans", 30, QFont.Weight.Bold))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "R")
    p.end()
    return QIcon(pix)


HYPR_RULE = "riko-avatar-runtime"


def _hyprland_rule(width, height):
    """Float + size + pin her through a runtime window rule (Hyprland 0.55+, Lua config).

    Doing it by rule (before the window maps) matters: resizing a mapped-and-tiled
    Qt window into a float leaves QtWebEngine's Wayland surface too short, with an
    opaque black strip at the bottom (as of Qt 6.12). Call this before creating the
    window. Best effort: on failure the caller falls back to dispatchers.
    """
    if not (os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl")):
        return False
    spec = (f'hl.window_rule({{ name = "{HYPR_RULE}", match = {{ class = "^({APP_ID})$" }}, '
            f'float = true, pin = true, size = "{width} {height}", '
            f'move = "monitor_w-{width + 20} monitor_h-{height + 20}", '
            f'border_size = 0, no_shadow = true, no_blur = true, rounding = 0, no_anim = true }})')
    return subprocess.run(["hyprctl", "eval", spec], capture_output=True, timeout=3).returncode == 0


def _find_own_window():
    try:
        out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=3).stdout
        return next((c for c in json.loads(out) if c.get("class") == APP_ID), None)
    except Exception:
        return None


def _hyprland_setup(width, height, ruled):
    """Make sure the window actually ended up floating, sized and pinned (best effort).

    The runtime rule normally does all of this at map time. This pass only fixes what
    differs, and for classic-config Hyprland (no `hyprctl eval`) it falls back to
    dispatchers with the old syntax.
    """
    if not (os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl")):
        return False

    def dispatch(expr):
        return subprocess.run(["hyprctl", "dispatch", expr], capture_output=True, timeout=3).returncode == 0

    win = None
    for _ in range(10):  # give the compositor a moment to map the window
        win = _find_own_window()
        if win:
            break
        time.sleep(0.3)
    if not win:
        return False
    w = f'address:{win["address"]}'
    if ruled:  # the rule should have done everything; only correct anything that did not stick
        if not win["floating"]:
            dispatch(f'hl.dsp.window.float({{action="enable", window="{w}"}})')
        if list(win["size"]) != [width, height]:
            dispatch(f'hl.dsp.window.resize({{x={width}, y={height}, window="{w}"}})')
        if not win["pinned"]:
            dispatch(f'hl.dsp.window.pin({{window="{w}"}})')
        return True
    calls = [(f'hl.dsp.window.float({{action="enable", window="{w}"}})',  # 0.55+, Lua config
              f'hl.dsp.window.resize({{x={width}, y={height}, window="{w}"}})',
              f'hl.dsp.window.pin({{window="{w}"}})'),
             (f'"setfloating 1,class:^({APP_ID})$"',  # classic hyprland.conf syntax, 0.54 and older
              f'"resizewindowpixel exact {width} {height},class:^({APP_ID})$"',
              f'"pin,class:^({APP_ID})$"')]
    for attempt in calls:
        if dispatch(attempt[0]):  # first call ok means this is the right syntax for this Hyprland
            for expr in attempt[1:]:
                dispatch(expr)
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Riko as a floating desktop avatar.")
    parser.add_argument("--asr", choices=["remote", "local", "groq"], help="speech to text (overrides the config)")
    parser.add_argument("--tts", choices=["none", "openai", "sovits", "fish"], help="her voice (overrides the config)")
    args = parser.parse_args()

    # Line-buffer stdout even when piped to a log file, so tail -f shows things as they happen.
    sys.stdout.reconfigure(line_buffering=True)

    cfg = load_config()
    cfg["asr"] = args.asr or cfg["asr"]
    cfg["tts"] = args.tts or cfg["tts"]

    llm.check(cfg)
    asr.check(cfg, fatal=False)  # typing still works if the ASR box is down
    tts.check(cfg)
    viewer.start(cfg)
    session = Session(cfg)
    print(f"\n=== Riko desktop  |  model: {llm.model_name()}  |  ears: {cfg['asr']}  |  voice: {cfg['tts']} ===")

    QGuiApplication.setDesktopFileName(APP_ID)  # becomes the Wayland app_id
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_ID)
    app.setQuitOnLastWindowClosed(False)  # hiding the window keeps her running in the tray
    signal.signal(signal.SIGINT, lambda *_: app.quit())  # Ctrl+C in the terminal
    keepalive = QTimer()  # lets Python see Ctrl+C while Qt's loop runs
    keepalive.start(250)
    keepalive.timeout.connect(lambda: None)

    # Ask the compositor to float/size/pin her before the window maps (Hyprland only;
    # mapping tiled and resizing afterwards leaves QtWebEngine's surface short).
    d = cfg.get("desktop") or {}
    ruled = _hyprland_rule(int(d.get("width", 420)), int(d.get("height", 680)))
    win = AvatarWindow(cfg)
    bridge = Bridge()

    def show_window():
        win.show()
        win.raise_()
        win.activateWindow()

    def toggle_window():
        win.hide() if win.isVisible() else show_window()

    def on_command(cmd):  # runs on the Qt thread
        if cmd == "type":
            show_window()
            win.input_open = True
            win.js("window.riko && riko.openInput()")
        elif cmd == "hide-input":
            win.js("window.riko && riko.closeInput()")
        elif cmd in ("toggle", "show", "hide"):
            {"toggle": toggle_window, "show": show_window, "hide": win.hide}[cmd]()
        elif cmd == "quit":
            app.quit()

    bridge.command.connect(on_command, Qt.ConnectionType.QueuedConnection)

    def control(cmd):  # runs on the HTTP thread
        if cmd == "talk":
            session.talk_toggle()
        elif cmd == "talk-start":
            session.talk_start()
        elif cmd == "talk-stop":
            session.talk_stop()
        elif cmd == "forget":
            session.forget()
        elif cmd == "input-opened":
            win.input_open = True
            print("[ctl] input opened", flush=True)
        elif cmd == "input-closed":
            win.input_open = False
            print("[ctl] input closed", flush=True)
        elif cmd == "status":
            return {"ok": True, "recording": session.recorder.recording, "visible": win.isVisible()}
        elif cmd in ("type", "hide-input", "toggle", "show", "hide", "quit"):
            bridge.command.emit(cmd)
        else:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        return {"ok": True}

    viewer.on_say(session.submit_text)
    viewer.on_control(control)

    # Tray menu (shows up if your bar has a tray, e.g. waybar / DMS)
    tray = QSystemTrayIcon(_tray_icon())
    tray.setToolTip("Riko")
    menu = QMenu()
    for label, fn in (("Type to her", lambda: on_command("type")),
                      ("Talk / stop talking", session.talk_toggle),
                      ("Show / hide", toggle_window),
                      ("Forget the chat", session.forget),
                      ("Quit", app.quit)):
        action = QAction(label, menu)
        action.triggered.connect(fn)
        menu.addAction(action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: toggle_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()

    show_window()
    QTimer.singleShot(400, lambda: _hyprland_setup(int(d.get("width", 420)), int(d.get("height", 680)), ruled))

    code = app.exec()
    if session.recorder.recording:
        session.recorder.stop()
    print("Bye!")
    sys.exit(code)


if __name__ == "__main__":
    main()
