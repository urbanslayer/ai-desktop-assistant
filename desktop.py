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
import os
import shutil
import signal
import subprocess
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from riko import asr, llm, tts, viewer
from riko.config import load_config
from riko.session import Session

APP_ID = "riko"  # Wayland app_id / X11 class; window rules match on this


class Bridge(QObject):
    """Carries commands from the HTTP thread (riko-ctl, the page) onto the Qt thread."""
    command = Signal(str)


class AvatarWindow(QWebEngineView):
    def __init__(self, cfg):
        super().__init__()
        d = cfg.get("desktop") or {}
        self.input_open = False
        self._drag_target = None

        self.setWindowTitle("Riko")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
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


def _hyprland_setup(width, height):
    """On Hyprland, float + pin the window and drop its border/shadow (best effort; window rules do it better)."""
    if not (os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl")):
        return
    w = f"class:^({APP_ID})$"
    for dispatcher, arg in (("setfloating", w),
                            ("resizewindowpixel", f"exact {width} {height},{w}"),
                            ("pin", w)):
        subprocess.run(["hyprctl", "dispatch", dispatcher, arg], capture_output=True, timeout=3)


def main():
    parser = argparse.ArgumentParser(description="Riko as a floating desktop avatar.")
    parser.add_argument("--asr", choices=["remote", "local", "groq"], help="speech to text (overrides the config)")
    parser.add_argument("--tts", choices=["none", "openai", "sovits", "fish"], help="her voice (overrides the config)")
    args = parser.parse_args()

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
        elif cmd == "input-closed":
            win.input_open = False
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
    d = cfg.get("desktop") or {}
    QTimer.singleShot(400, lambda: _hyprland_setup(int(d.get("width", 420)), int(d.get("height", 680))))

    code = app.exec()
    if session.recorder.recording:
        session.recorder.stop()
    print("Bye!")
    sys.exit(code)


if __name__ == "__main__":
    main()
