"""Riko on your desktop: a borderless, see-through window where she just hovers.

    uv run desktop.py

Keys (only while her window is focused — app-specific, and never the Super/Win
key, which belongs to your window manager). Configure them under
desktop.shortcuts in character_config.yaml; defaults:
    Enter or /               type to her (Enter sends, Esc closes)
    Space (hold)             talk, release to send (push-to-talk, your local mic)
    W                        she waves at you
    P                        next persona
    H                        hide her (the tray icon brings her back)
    Ctrl+Shift+Backspace     forget the chat (press again within 3 s to confirm)
    Ctrl+Q                   quit (also in the tray menu)
With the mouse:
    drag            move the window          right-drag  turn her around
    shift-drag      move her inside it       scroll      zoom

Want the same from any window? Those are optional, not part of the app:
bin/riko-ctl hits her local port from a keybind or script (see its --help),
and desktop/hyprland/ ships example Hyprland binds that stay off Super too.
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
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from riko import asr, client_tools, hub_client, shortcuts, tts, viewer
from riko.config import load_config
from riko.session import Session

APP_ID = "riko"  # Wayland app_id / X11 class; window rules match on these

# Commands that need the Qt thread (they poke the page or the window). 'wave'
# asks the page's director for a social wave (same one the W shortcut fires).
PAGE_CMDS = {"type", "hide-input", "toggle", "show", "hide", "quit", "wave"}
WAVE_JS = "window.riko && riko.requestWave && riko.requestWave()"


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
        # No QShortcut here: every key, quit included, goes through the page and
        # its configurable shortcut map (desktop.shortcuts) — one source of truth.

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
    parser.add_argument("--tts", choices=["hub", "none", "openai", "sovits", "fish"], help="her voice (overrides the config)")
    args = parser.parse_args()

    # Line-buffer stdout even when piped to a log file, so tail -f shows things as they happen.
    sys.stdout.reconfigure(line_buffering=True)

    cfg = load_config()
    cfg["asr"] = args.asr or cfg["asr"]
    cfg["tts"] = args.tts or cfg["tts"]

    hub = hub_client.check(cfg)  # starts the hub in-process in local mode
    asr.check(cfg, fatal=False)  # typing still works if the ASR box is down
    tts.check(cfg)
    viewer.start(cfg)
    session = Session(cfg, source="desktop")
    # Client-tool channel (docs/ROUNDS.md R2-F): register on the hub, run the
    # tools it asks for, show approvals, and serve the Riko Helper Firefox
    # extension over a local token-authenticated WebSocket.
    runtime, browser = client_tools.start_desktop(cfg, hub_client.get())
    hub_client.start_state_poller()  # notice switches made from the web page or another PC
    session.greet_on_start()  # she says hello once when the page is ready (desktop.greet_on_start)
    active = hub_client.active_persona() or {}
    print(f"\n=== Riko desktop  |  hub: {hub.url}  |  persona: {active.get('name', '?')}"
          f"  |  ears: {cfg['asr']}  |  voice: {cfg['tts']} ===")

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
        elif cmd == "wave":
            win.js(WAVE_JS)
        elif cmd in ("toggle", "show", "hide"):
            {"toggle": toggle_window, "show": show_window, "hide": win.hide}[cmd]()
        elif cmd == "quit":
            app.quit()

    bridge.command.connect(on_command, Qt.ConnectionType.QueuedConnection)

    def control(cmd, args=None):  # runs on the HTTP thread; args is the JSON body
        args = args or {}
        if cmd == "talk":
            session.talk_toggle()
        elif cmd == "talk-start":
            session.talk_start()
        elif cmd == "talk-stop":
            session.talk_stop()
        elif cmd == "forget":
            session.forget()
        elif cmd == "persona":
            name = str(args.get("name", "")).strip()
            if not name:
                return {"ok": False, "error": "persona: give a name, e.g. riko-ctl persona asuka"}
            try:
                active = session.switch_persona(name)  # speaks the greeting on the worker
            except hub_client.HubError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "persona": active["name"]}
        elif cmd == "persona-next":
            try:
                active = session.persona_next()  # cycle to the next persona (wraps)
            except hub_client.HubError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "persona": active["name"]}
        elif cmd == "personas":
            try:
                listed = hub_client.get().personas()
            except hub_client.HubError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True,
                    "active": (hub_client.active_persona() or {}).get("id"),
                    "personas": [{"id": p["id"], "name": p["name"]} for p in listed]}
        elif cmd == "input-opened":
            win.input_open = True
            print("[ctl] input opened", flush=True)
        elif cmd == "input-closed":
            win.input_open = False
            print("[ctl] input closed", flush=True)
        elif cmd == "status":
            return {"ok": True, "recording": session.recorder.recording, "visible": win.isVisible()}
        elif cmd in ("approve", "deny"):
            if runtime is None:
                return {"ok": False, "error": "client tools are off (desktop.client.enabled: false)"}
            return runtime.approve_hook() if cmd == "approve" else runtime.deny_hook()
        elif cmd == "approval-pending":
            return {"ok": True, "pending": runtime.pending_approval() if runtime else None}
        elif cmd == "browser-status":
            on = bool(browser and browser.connected())
            return {"ok": True, "enabled": runtime is not None, "connected": on,
                    "extension": browser.extension_name() if on else "",
                    "ws_port": (d.get("client") or {}).get("browser_port",
                                                           client_tools.DEFAULT_BROWSER_PORT)}
        elif cmd == "browser-token":
            return {"ok": True, "token": client_tools.browser_token(),
                    "port": (d.get("client") or {}).get("browser_port",
                                                        client_tools.DEFAULT_BROWSER_PORT),
                    "connected": bool(browser and browser.connected())}
        elif cmd == "history-import":
            msgs = args.get("messages")
            if not isinstance(msgs, list) or not msgs:
                return {"ok": False, "error": 'history-import: send {"messages": [{"role","content"}, ...]}'}
            try:
                n = hub_client.get().import_history(msgs)
            except hub_client.HubError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "imported": n}
        elif cmd in PAGE_CMDS:  # type / hide-input / toggle / show / hide / quit / wave
            bridge.command.emit(cmd)
        else:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        return {"ok": True}

    viewer.on_say(session.submit_text)
    viewer.on_control(control)

    # Tray menu (shows up if your bar has a tray, e.g. waybar / DMS).
    # Labels name the configured keys; viewer.start() above already resolved the
    # same map and printed any warnings, so this pass stays quiet.
    sc, _ = shortcuts.resolve(d.get("shortcuts"), warn=False)

    def keys(action):
        return ", ".join(sc.get(action) or [])

    def hint(action, verb=""):  # ' (hold Space)' / ' (Ctrl+Q)' / '' when disabled
        k = keys(action)
        return f"  ({verb}{k})" if k else ""

    tray = QSystemTrayIcon(_tray_icon())
    tray.setToolTip("Riko")
    menu = QMenu()

    # Persona submenu: rebuilt every time it opens, so it tracks the hub
    # (keyword switches, the web page, another PC) without any polling here.
    persona_menu = QMenu("Persona", menu)

    def rebuild_persona_menu():
        persona_menu.clear()
        try:
            listed = hub_client.get().personas()
            active_id = (hub_client.active_persona() or {}).get("id")
        except hub_client.HubError as e:
            dead = QAction(f"hub unreachable: {e}", persona_menu)
            dead.setEnabled(False)
            persona_menu.addAction(dead)
            return
        for p in listed:
            def pick(pid=p["id"], _=False):
                try:
                    session.switch_persona(pid)
                except hub_client.HubError as e:
                    print(f"[persona] {e}")
            action = QAction(p["name"], persona_menu)
            action.setCheckable(True)
            action.setChecked(p["id"] == active_id)
            action.triggered.connect(pick)
            persona_menu.addAction(action)

    persona_menu.aboutToShow.connect(rebuild_persona_menu)
    rebuild_persona_menu()
    menu.addMenu(persona_menu)
    for label, fn in ((f"Type to her{hint('type')}", lambda: on_command("type")),
                      (f"Talk / stop talking{hint('talk', 'hold ')}", session.talk_toggle),
                      (f"Show / hide{hint('hide')}", toggle_window),
                      (f"Forget the chat{hint('forget')}", session.forget),
                      (f"Quit{hint('quit')}", app.quit)):
        action = QAction(label, menu)
        action.triggered.connect(fn)
        menu.addAction(action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: toggle_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    # notify-send missing? the tool falls back to a tray bubble (and if there is
    # no tray at all, to the terminal).
    client_tools.set_notify_fallback(
        lambda title, text: tray.showMessage(title, text, 8000)
        if tray.isVisible() else print(f"[notify] {title}: {text}"))

    def show_browser_token():  # the pairing token, shown in the tray as promised
        if browser is None:
            tray.showMessage("Riko", "browser tools are off (desktop.client.enabled: false)")
            return
        info = control("browser-token")
        tray.showMessage("Riko — Firefox helper",
                         f"token: {info['token']}\nport: {info['port']}\n"
                         f"extension: {'connected' if info['connected'] else 'not connected'}",
                         15000)

    token_action = QAction("Show browser token", menu)
    token_action.triggered.connect(show_browser_token)
    menu.addAction(token_action)
    tray.setContextMenu(menu)

    show_window()
    QTimer.singleShot(400, lambda: _hyprland_setup(int(d.get("width", 420)), int(d.get("height", 680)), ruled))

    code = app.exec()
    if session.recorder.recording:
        session.recorder.stop()
    if runtime is not None:
        runtime.stop()  # unregister from the hub's client channel
    print("Bye!")
    sys.exit(code)


if __name__ == "__main__":
    main()
