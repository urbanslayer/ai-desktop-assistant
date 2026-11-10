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
    L                        push-to-talk <-> always listen (hands-free VAD)
    Ctrl+Shift+Backspace     forget the chat (press again within 3 s to confirm)
    Ctrl+Q                   quit (also in the tray menu)
With the mouse:
    drag            move the window          right-drag  turn her around
    shift-drag      move her inside it       scroll      size her up/down
                                                  (the window grows with her,
                                                   feet stay put, fits the
                                                   monitor; remembered per
                                                   persona)
    Ctrl+scroll     camera close-up          dblclick    reset size + camera
                                                  (the tray has Reset size too)

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
import threading
import time
import traceback
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QInputDialog, QLineEdit, QMenu,
                               QSystemTrayIcon)

from riko import appstate, asr, avatar_scale, client_tools, hub_client, shortcuts, tts, viewer
from riko import roam as roam_state
from riko.config import load_config
from riko.session import Session

APP_ID = "riko"  # Wayland app_id / X11 class; window rules match on these

# Commands that need the Qt thread (they poke the page or the window). 'wave'
# asks the page's director for a social wave (same one the W shortcut fires).
PAGE_CMDS = {"type", "hide-input", "toggle", "show", "hide", "quit", "wave", "reset-size"}
WAVE_JS = "window.riko && riko.requestWave && riko.requestWave()"


class Bridge(QObject):
    """Carries commands from the HTTP thread (riko-ctl, the page) onto the Qt thread."""
    command = Signal(str)


def make_approval_key_handler(win, runtime):
    """Answer an open approval from her window on key RELEASE, not press.

    The owner approves with the physical Enter ('Enter = yes, Esc = no').
    The Wayland seat tracks pressed keycodes across ALL keyboards: if the
    approval resolves on the press, the typing engine re-focuses the target
    and injects a virtual Enter within milliseconds — while the physical
    keycode 28 is still down — and the seat merges them away, so the
    terminal sees 'focus entered with Enter already down' and the command
    never runs (owner failure 2026-11-09). Resolving on the RELEASE puts
    the physical key up before the engine's settle even starts, and the
    engine waits a further settle delay before it types.

    The raw Qt events reach us through the eventFilter the window already
    installs on the render widget; swallowing the press here means the page
    never sees these keys while an approval is open (its keydown Enter→yes
    fallback stays for the no-Qt/web case). Returns True = event consumed.
    """
    def handler(event) -> bool:
        if runtime is None or getattr(win, "input_open", False):
            return False            # the text box owns its own keys
        key = event.key()
        if key not in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            return False
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.AltModifier
                                | Qt.KeyboardModifier.MetaModifier):
            return False
        if runtime.pending_approval() is None:
            return False
        if event.type() == QEvent.Type.KeyPress:
            return True             # swallow: the answer waits for the release
        if key == Qt.Key.Key_Escape:
            runtime.deny_hook()
        else:
            runtime.approve_hook()
        return True
    return handler


class AvatarPage(QWebEnginePage):
    """Repeats the page's console in our terminal, so a broken script here is visible there."""

    NAMES = {"InfoMessageLevel": "log", "WarningMessageLevel": "warning", "ErrorMessageLevel": "error"}

    def javaScriptConsoleMessage(self, level, message, line, source):
        name = self.NAMES.get(getattr(level, "name", ""), "log")
        print(f"[js:{name}] {message}  ({source}:{line})", flush=True)


class AvatarWindow(QWebEngineView):
    def __init__(self, cfg, scale: float = avatar_scale.SCALE_DEFAULT):
        super().__init__()
        # setPage() rather than the QWebEngineView(page) constructor: through the
        # constructor PySide6 keeps the view's own default page and our console
        # forwarding below never runs.
        self.setPage(AvatarPage(self))
        d = cfg.get("desktop") or {}
        self.input_open = False
        self.approval_keys: Optional[Callable[[object], bool]] = None  # set by main(): answer approvals on RELEASE
        self._drag_target = None
        self.on_focus: Optional[Callable[[], None]] = None  # chat.read report on focus (docs/TIMERS.md D)

        self.setWindowTitle("Riko")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Without this the render widget gets its own native window, which breaks
        # the translucent background and leaves a black strip on resized Wayland windows.
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        # Open at the persona's saved scroll-to-size (avatar_scale.json):
        # base window x scale, same mapping the page's wheel drives later.
        s = avatar_scale.clamp_scale(scale)
        self.resize(round(int(d.get("width", 420)) * s),
                    round(int(d.get("height", 680)) * s))

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
        if (event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                and self.approval_keys is not None
                and self.approval_keys(event)):
            return True   # approval answered on key RELEASE (seat collision)
        if (event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and not getattr(self, "roam_strip", False)   # strip: drags move HER, not the window
                and not (self.input_open and event.position().y() > self.height() - 80)):
            self.windowHandle().startSystemMove()
            return True
        return super().eventFilter(obj, event)

    def js(self, code):
        self.page().runJavaScript(code)

    def changeEvent(self, event):
        # docs/TIMERS.md D: seeing her chat is how chat.read timers cancel —
        # report on focus (throttled inside hub_client.note_chat_read).
        if event.type() == QEvent.Type.WindowActivate and self.on_focus:
            try:
                self.on_focus()
            except Exception as e:  # a hub hiccup must not eat the event
                print(f"[hub] on_focus: {type(e).__name__}: {e}")
        super().changeEvent(event)


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


# ── Roam strip (docs/WALK_MEMORY.md "W") ─────────────────────────────────
# desktop.roam: strip turns the window into a full-width see-through band at
# the BOTTOM edge of the screen: she wanders along it and everything around
# her is click-through — only her body (the page's /control/bbox posts,
# ~10 Hz) catches clicks. Method selection, per the contract:
#   1. wlr-layer-shell + shaped input region — needs a layer-shell shell
#      plugin inside the app's own Qt AND a per-window input-region API.
#      Probed at startup (see _layer_shell_probe); PySide6's bundled Qt
#      builds (6.12 as of writing) ship no layer-shell plugin, and Qt has no
#      public QWindow input-region API, so this path is normally unusable.
#   2. XWayland (xcb): an override-redirect, frameless top-level (Qt's
#      BypassWindowManagerHint) that no WM repositions, placed by
#      XMoveResizeWindow along the bottom edge, click-through shaped with
#      XFixes input regions. This is the method that actually works today.
#   3. Neither usable → keep the small window and print a clear message.

class _XShape:
    """The few xcb calls the strip needs, via ctypes (no extra deps)."""

    def __init__(self):
        import ctypes
        import ctypes.util
        self.ct = ctypes
        xcb = ctypes.util.find_library("xcb")
        if not xcb:
            raise RuntimeError("libxcb not loadable")
        self.lib = ctypes.CDLL(xcb)
        # SHAPE/XFIXES live in their own libs on Linux, not in libxcb itself.
        # find_library only maps SONAMEs here, so try the soname. CDLL has no
        # symbol enumeration, so pull the exact symbols we call.
        need = {"xcb-shape": ("xcb_shape_query_version", "xcb_shape_query_version_reply"),
                "xcb-xfixes": ("xcb_xfixes_query_version", "xcb_xfixes_query_version_reply",
                               "xcb_xfixes_create_region",
                               "xcb_xfixes_destroy_region",
                               "xcb_xfixes_set_window_shape_region",
                               "xcb_xfixes_get_cursor_image")}
        for name, syms in need.items():
            p = ctypes.util.find_library(name) or f"lib{name}.so.0"
            try:
                extra = ctypes.CDLL(p)
            except OSError:
                continue
            for sym in syms:
                try:
                    setattr(self.lib, sym, getattr(extra, sym))
                except AttributeError:
                    pass
        # xcb Rect { int16 x, y; uint16 w, h; }
        self.Rect = type("Rect", (ctypes.Structure,), {"_fields_": [
            ("x", ctypes.c_int16), ("y", ctypes.c_int16),
            ("w", ctypes.c_uint16), ("h", ctypes.c_uint16)]})
        self.fixes = None
        self.display = None
        # fail fast if the shaping symbols are not reachable at all
        for sym in ("xcb_xfixes_create_region", "xcb_xfixes_set_window_shape_region",
                    "xcb_generate_id", "xcb_flush"):
            getattr(self.lib, sym)   # AttributeError -> caller falls back

    def connect(self):
        import ctypes
        # restype BEFORE the first call: default c_int truncates the 64-bit
        # connection pointer and the next call dereferences garbage (segfault)
        self.lib.xcb_connect.restype = ctypes.c_void_p
        self.lib.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.xcb_connection_has_error.argtypes = [ctypes.c_void_p]
        self.display = self.lib.xcb_connect(None, None)
        if not self.display or self.lib.xcb_connection_has_error(self.display):
            self.display = None
            raise RuntimeError("xcb connect failed")
        print("[roam] xcb connected (XWayland)", flush=True)
        # ENABLE SHAPE + XFIXES: libxcb keeps an extension's major opcode at 0
        # until the client round-trips its version — requests sent before that
        # are silent no-ops (the nested-sway smoke proved the shaped-input
        # region never reached the server without this handshake).
        class _Cookie(ctypes.Structure):
            _fields_ = [("sequence", ctypes.c_uint32)]
        for q, maj, min in (("xcb_shape_query_version", 1, 1),
                            ("xcb_xfixes_query_version", 5, 0)):
            try:
                req = getattr(self.lib, q)
            except AttributeError:
                continue
            req.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8]
            req.restype = _Cookie
            rep = getattr(self.lib, q + "_reply")
            rep.argtypes = [ctypes.c_void_p, _Cookie, ctypes.POINTER(ctypes.c_void_p)]
            rep.restype = ctypes.c_void_p
            # Ask for the NEWEST version (the server clamps to its own): with
            # XFIXES this is load-bearing — XFixesSetWindowShapeRegion arrived
            # in 2.0, and a 1.x negotiation makes the server reject the set
            # with BadValue (nested-sway smoke: mask silently stayed full).
            rep(self.display, req(self.display, maj, min), None)   # round trip = enable
        self.shape = True
        self.fixes = True

        self.lib.xcb_generate_id.argtypes = [ctypes.c_void_p]
        self.lib.xcb_generate_id.restype = ctypes.c_uint32
        self.lib.xcb_xfixes_create_region.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        self.lib.xcb_xfixes_destroy_region.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.lib.xcb_xfixes_set_window_shape_region.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8, ctypes.c_int16, ctypes.c_int16, ctypes.c_uint32]
        self.lib.xcb_flush.argtypes = [ctypes.c_void_p]
        self._region = None

    def create_region(self, rects):
        """rects: list of (x, y, w, h). Returns a region id (caller destroys)."""
        if not rects:
            return 0
        arr = (self.Rect * len(rects))(*[self.Rect(x, y, w, h) for x, y, w, h in rects])  # type: ignore[operator]
        rid = self.lib.xcb_generate_id(self.display)
        self.lib.xcb_xfixes_create_region(self.display, rid, len(rects), arr)
        return rid

    def destroy_region(self, rid):
        if rid:
            self.lib.xcb_xfixes_destroy_region(self.display, rid)

    def set_input_region(self, xid, region):
        # ShapeSO = 0, so Kind = "bounding" is 0; "Input" kind for SHAPE is 1
        # (xcb shape: 0 bounding, 1 clip, 2 input... X ShapeInput = 2)
        self.lib.xcb_xfixes_set_window_shape_region(self.display, xid, 2, 0, 0, region)
        self.lib.xcb_flush(self.display)


def _layer_shell_probe():
    """(usable, why) for the layer-shell method from this Qt build."""
    try:
        import PySide6
        q = os.path.join(os.path.dirname(PySide6.__file__), "Qt", "plugins",
                         "wayland-shell-integration")
        plugins = [f for f in os.listdir(q) if "layer" in f.lower()] if os.path.isdir(q) else []
    except Exception:
        plugins = []
    if not plugins:
        return False, ("Qt ships no layer-shell shell-integration plugin "
                       f"(looked in {q!r})")
    return False, ("layer-shell plugin present, but Qt exposes no per-window "
                   "input-region API — shaped click-through needs XWayland")


class RoamController(QObject):
    """Owns strip placement + the click-through input region (docs/WALK_MEMORY.md W)."""

    def __init__(self, win, cfg, tray=None, scale: float = avatar_scale.SCALE_DEFAULT):
        super().__init__()
        d = cfg.get("desktop") or {}
        self.win = win
        self.tray = tray
        self.enabled = str(d.get("roam", "off")).lower() == "strip"
        self.want = str(d.get("roam_backend", "auto")).lower()  # auto | layer | xcb
        self.backend = None            # None | "xcb" ("layer" reserved)
        self.in_strip = False
        self.x = None                  # (x, y, w, h) window coords of her body
        self._sent = None
        self.xcb = None
        self.xid = 0
        self.warned = ""
        self._printed = False
        self._last_print = 0.0
        self.filter: object = None  # keepalive ref: Qt does NOT own the filter object
        self.on_leave: Optional[Callable[[], None]] = None  # main(): restore the scaled small window
        # strip size: full screen width, the base window height x the avatar
        # scale — bigger her, taller the band — clamped by
        # riko/roam.strip_geometry (min height, 60% of the screen so a tall
        # monitor keeps a visible "band" feel) and anchored to the bottom edge
        self.base_h = int(d.get("height", 680))
        self.scale = avatar_scale.clamp_scale(scale)
        scr = QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        self.strip_geom = roam_state.strip_geometry(
            (geo.x(), geo.y(), geo.width(), geo.height()), self.base_h, self.scale)
        self.strip_h = self.strip_geom[3]
        # native events (X11 ButtonPress forwarding) only matter on the xcb path
        self._bypass = False

    def set_scale(self, scale):
        """Avatar scale changed (scroll): the strip grows/shrinks with her —
        full width, bottom edge anchored, height = base x scale (clamped).
        walk bounds and the click-through mask follow on the next page
        posts (the window resize itself re-triggers them)."""
        self.scale = avatar_scale.clamp_scale(scale)
        scr = QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        self.strip_geom = roam_state.strip_geometry(
            (geo.x(), geo.y(), geo.width(), geo.height()), self.base_h, self.scale)
        self.strip_h = self.strip_geom[3]
        if self.in_strip and self.backend == "xcb":
            x, y, w, h = self.strip_geom
            self.win.setFixedSize(w, h)
            self.win.resize(w, h)
            self._place(x, y, w, h)
            self.apply_bbox()

    # ── method selection ──────────────────────────────────────────────
    def pick_backend(self):
        if self.want == "layer":
            ok, why = _layer_shell_probe()
            self._fallback(f"roam_backend: layer is not usable ({why}); keeping the small window")
            return None
        if self.want in ("auto", "xcb"):
            ok, why = _layer_shell_probe()
            if ok:
                self.backend = "layer"      # reserved; no Qt input-region API today
                return self.backend
            print(f"[roam] layer-shell method not workable: {why} -> XWayland (xcb)",
                  flush=True)
            try:
                self.xcb = _XShape()
                self.xcb.connect()
                self.backend = "xcb"
                return "xcb"
            except Exception as e:  # noqa: BLE001
                if self.want == "xcb":
                    self._fallback(f"xcb strip failed: {e}")
                else:
                    self._fallback(f"no usable strip backend (layer-shell unusable; {e})")
        return None

    def _fallback(self, why):
        if self.warned == why:
            return
        self.warned = why
        print(f"[roam] roam strip OFF: {why}", flush=True)
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage("Riko", f"roam strip off — {why}", 8000)

    # ── entering / leaving the strip ──────────────────────────────────
    def strip_flags(self):
        return (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.BypassWindowManagerHint)

    def enter_strip(self):
        if not self.enabled or self.in_strip:
            return
        if not self.backend and not self.pick_backend():
            return
        if self.backend == "xcb":
            x, y, w, h = self.strip_geom
            # recreate the platform window with bypass-WM flags, sized before show
            print("[roam] enter_strip: flags", flush=True)
            self.win.setWindowFlags(self.strip_flags())
            self.win.setFixedSize(w, h)
            self.win.resize(w, h)
            print("[roam] enter_strip: show", flush=True)
            self.win.show()
            print("[roam] enter_strip: winId", flush=True)
            self.xid = int(self.win.windowHandle().winId())
            print(f"[roam] enter_strip: place xid={self.xid:x}", flush=True)
            self._place(x, y, w, h)
            self.apply_bbox()          # empty region until the page posts one
            # focus so keyboard shortcuts still reach her (no WM to do it for us)
            self._focus()
            self.in_strip = True
            print(f"[roam] strip ON ({self.backend}): {w}x{h} at ({x},{y}); "
                  "click-through everywhere except her body", flush=True)

    def leave_strip(self):
        if not self.in_strip:
            return
        self.in_strip = False
        d = self.win.property("riko_normal_flags")
        flags = d if d is not None else (Qt.WindowType.FramelessWindowHint
                                         | Qt.WindowType.WindowStaysOnTopHint)
        self.win.setWindowFlags(Qt.WindowType(flags))
        self.win.setFixedSize(16777215, 16777215)   # QWIDGETSIZE_MAX: free resize again
        self._clear_region()
        self.win.show()
        if self.on_leave:
            self.on_leave()   # main(): back to the scaled small window, not full width
        print("[roam] back to the small window", flush=True)

    # ── X plumbing ─────────────────────────────────────────────────────
    def _place(self, x, y, w, h):
        if not (self.xcb and self.xcb.display and self.xid):
            return
        self.xcb.lib.xcb_configure_window.argtypes = [
            self.xcb.ct.c_void_p, self.xcb.ct.c_uint32, self.xcb.ct.c_uint32,
            self.xcb.ct.c_void_p]

        class_vals = (self.xcb.ct.c_uint32 * 7)()
        # values order per mask bits: x, y, width, height, border, sibling, stack
        for i, v in enumerate([x, y, w, h, 0, 0, 0]):
            class_vals[i] = v
        mask = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 6)  # x,y,w,h,above
        self.xcb.lib.xcb_configure_window(self.xcb.display, self.xid, mask,
                                          self.xcb.ct.addressof(class_vals))
        self.xcb.lib.xcb_flush(self.xcb.display)

    def _focus(self):
        if not (self.xcb and self.xcb.display and self.xid):
            return
        lib = self.xcb.lib
        lib.xcb_set_input_focus.argtypes = [self.xcb.ct.c_void_p, self.xcb.ct.c_uint8,
                                            self.xcb.ct.c_uint32, self.xcb.ct.c_uint32]
        lib.xcb_set_input_focus(self.xcb.display, 1, self.xid, 0)  # revert_to=PointerRoot
        lib.xcb_flush(self.xcb.display)

    def apply_bbox(self):
        """Shape the window's input region to her projected body (or nothing).

        Re-applied on EVERY page update, not just on change: Qt may rewrite
        window shapes on its own map/expose cycles, and a single rejected or
        lost request (e.g. during window transitions) must not leave a stale
        full-window mask forever. One round trip at 10 Hz — negligible.
        """
        if not (self.in_strip and self.backend == "xcb" and self.xcb and self.xcb.display):
            return
        rect = self._clipped_bbox()
        changed = rect != self._sent
        self._sent = rect
        new = self.xcb.create_region([rect] if rect else [])
        self.xcb.set_input_region(self.xid, new)
        self.xcb.destroy_region(new)
        # Rate-limited trace of every shaping change (≤ 1 line / 2 s): the first
        # line proves the page is live, later lines prove the mask narrows to
        # her body — the nested-sway smoke asserts on these.
        if changed and (not self._printed or time.monotonic() - self._last_print >= 2.0):
            self._printed = True
            self._last_print = time.monotonic()
            print(f"[roam] input region -> {rect}", flush=True)

    def _clear_region(self):
        self._sent = None
        if self.in_strip or (self.xcb and self.xcb.display and self.xid):
            try:
                self.xcb.set_input_region(self.xid, 0)   # RegionNone: whole window
            except Exception:  # noqa: BLE001
                pass

    def _clipped_bbox(self):
        if not self.x or not self.in_strip:
            return None
        x, y, w, h = self.x
        _, _, sw, sh = self.strip_geom
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(sw, x + w), min(sh, y + h)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    # ── from the page / viewer ─────────────────────────────────────────
    def update_bbox(self, x, y, w, h):
        self.x = (int(x), int(y), int(w), int(h))
        self.apply_bbox()

    # ── click forwarding (xcb path) ────────────────────────────────────
    def native_event(self, event_type, message):
        """QAbstractNativeEventFilter hook; returns True if the event was consumed."""
        if self.backend != "xcb" or not self.in_strip:
            return False
        if bytes(event_type) != b"xcb_generic_event_t":
            return False
        try:
            import ctypes
            ev = ctypes.cast(message, ctypes.POINTER(ctypes.c_uint8))
            code = ev[0] & 0x7F
            if code not in (4, 5):          # ButtonPress / ButtonRelease
                return False
            # raw X11 ButtonPress wire layout (xcb_generic_event_t):
            # 0 type, 1 detail(button), 2 seq, 4 time, 8 root, 12 event, 16 child,
            # 20 root_x, 22 root_y, 24 event_x, 26 event_y (16-bit, event-relative)
            ex = int.from_bytes(bytes(ev[24:26]), "little")
            ey = int.from_bytes(bytes(ev[26:28]), "little")
        except Exception:  # noqa: BLE001
            return False
        rect = self._clipped_bbox()
        inside = bool(rect) and rect[0] <= ex < rect[0] + rect[2] and rect[1] <= ey < rect[1] + rect[3]
        kind = "pointerdown" if code == 4 else "pointerup"
        print(f"[roam] forward {kind} ({ex},{ey})", flush=True)
        if inside:
            self.win.js(f"window.__rikoClick && __rikoClick({kind!r}, {ex}, {ey})")
            self._focus()
        return bool(inside)


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


class _NativeFilter(QAbstractNativeEventFilter):
    """Routes X11 button events to the roam controller for click forwarding."""

    def __init__(self, roamc):
        super().__init__()
        self.roamc = roamc

    def nativeEventFilter(self, event_type, message):
        try:
            return self.roamc.native_event(event_type, message), 0
        except Exception:  # noqa: BLE001 — never eat the event queue on a parse slip
            return False, 0


def build_persona_menu(parent_menu, session):
    """The tray's Persona submenu (built by main(), split out so an offscreen
    Qt test can drive it). Rebuilt every time it opens, so it tracks the hub
    (keyword switches, the web page, another PC) without any polling here.

    Two tray-only traps, both owner-reported bugs:
    - ``triggered`` hands the action's ``checked`` bool to its slot, so the id
      must be bound in a keyword arg of the lambda; a closure default arg like
      ``def pick(pid=p['id'])`` silently receives True instead of the id
      (the hub then 404s on persona 'True').
    - checkable actions are only mutually exclusive inside a QActionGroup;
      without one, several personas stay ticked at once. The group is
      parented to the menu so it survives rebuilds (QAction's destructor
      removes a deleted action from its group).
    """
    persona_menu = QMenu("Persona", parent_menu)
    group = QActionGroup(persona_menu)
    group.setExclusive(True)

    def sync_checks():
        """Re-tick from the hub's ACTUAL active persona: a switch may have
        failed (hub down, 404), and Qt already moved the tick on click."""
        active_id = (hub_client.active_persona() or {}).get("id")
        for act in persona_menu.actions():
            if act.isCheckable():
                act.setChecked(act.data() == active_id)

    def pick(pid):
        if (hub_client.active_persona() or {}).get("id") == pid:
            sync_checks()  # clicking the active persona is a no-op
            return
        try:
            session.switch_persona(pid)
        except hub_client.HubError as e:
            print(f"[persona] {e}")
        sync_checks()

    def rebuild():
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
            action = QAction(p["name"], persona_menu)
            action.setCheckable(True)
            action.setData(p["id"])
            action.setChecked(p["id"] == active_id)
            # _checked swallows the triggered(checked) bool; pid is keyword-bound
            action.triggered.connect(lambda _checked=False, pid=p["id"]: pick(pid))
            group.addAction(action)
            persona_menu.addAction(action)

    persona_menu.aboutToShow.connect(rebuild)
    rebuild()
    parent_menu.addMenu(persona_menu)
    return persona_menu


def build_sense_action(menu, sense, apply_toggle):
    """The tray's 'Let her see my desktop' checkbox (docs/DESKTOP_SENSE.md),
    built apart from main() so an offscreen Qt test can pull the real slot.
    `apply_toggle(enabled) -> applied state` is the app's single awareness
    path: it flips + persists + re-registers the tools with the hub live.
    The slot swallows QAction.triggered's checked bool as its FIRST
    parameter (skill pitfall: it must not land in an id/argument slot) and
    re-ticks from the applied state, since Qt moves the tick before the
    toggle lands."""

    def toggle(checked=None):
        if sense is None:
            return
        want = (not sense.awareness.enabled) if checked is None else bool(checked)
        action.setChecked(bool(apply_toggle(want)))

    action = QAction("Let her see my desktop", menu)
    action.setCheckable(True)
    action.setToolTip("desktop awareness: your open windows, browser tabs "
                      "and screens, opening apps, and switching workspaces")
    action.setEnabled(sense is not None)
    action.triggered.connect(toggle)
    if sense is not None:
        action.setChecked(sense.awareness.enabled)
    return action


def build_dictation_action(menu, typing_svc, apply_toggle):
    """The tray's 'Dictation' checkbox (docs/TYPING.md §8): start/stop the
    mode that types her speech into the pinned window. Built apart from
    main() so an offscreen Qt test can pull the real slot; same checked-bool
    care as build_sense_action. `apply_toggle(enabled) -> applied state`."""

    def toggle(checked=None):
        if typing_svc is None:
            return
        want = (not typing_svc.dictation.on) if checked is None else bool(checked)
        action.setChecked(bool(apply_toggle(want)))

    action = QAction("Dictation", menu)
    action.setCheckable(True)
    action.setToolTip("dictation mode: what she hears is typed into the "
                      "window focused now (focus away = pause). Control "
                      "phrases: stop dictating / new line / new paragraph / "
                      "delete that")
    action.setEnabled(typing_svc is not None)
    action.triggered.connect(toggle)
    if typing_svc is not None:
        action.setChecked(typing_svc.dictation.on)
    return action


def sync_name_gate_action(action, session):
    """Keep the 'Only when I say her name' tick honest: checked from the
    session's state, enabled only while always-listen runs (the name gate
    never bites push-to-talk, so the box is greyed out there). Called at
    startup and on every listen-ui / namegate-ui bridge event."""
    action.blockSignals(True)
    action.setChecked(bool(session.name_gate))
    action.setEnabled(session.listen_mode == "always")
    action.blockSignals(False)


def build_name_gate_action(menu, session):
    """The tray's 'Only when I say her name' checkbox (docs/BACKLOG.md
    Planned 5): the always-listen name gate. Built apart from main() so an
    offscreen Qt test can pull the real slot; same checked-bool care as
    build_sense_action. The toggle goes through session.set_name_gate
    (persist + notify), so the tray, riko-ctl and the L-key redraw share
    one path — the tick lands via the namegate-ui bridge event."""

    def toggle(checked=None):
        want = (not session.name_gate) if checked is None else bool(checked)
        session.set_name_gate(want)

    action = QAction("Only when I say her name", menu)
    action.setCheckable(True)
    action.setToolTip("always-listen only: utterances that don't say the active "
                      "persona's name, a keyword or an alias are ignored "
                      "(push-to-talk is never gated; follow-ups right after a "
                      "reply pass without the name)")
    action.triggered.connect(toggle)
    sync_name_gate_action(action, session)
    return action


def build_stop_typing_action(menu, typing_svc):
    """'Stop typing' (docs/TYPING.md §5): aborts the keystroke batch in
    progress at the next chunk; dictation mode itself is untouched."""
    action = QAction("Stop typing", menu)
    action.setToolTip("abort the keystroke batch in progress right away")
    action.setEnabled(typing_svc is not None)
    action.triggered.connect(
        lambda checked=None: typing_svc.engine.stop() if typing_svc else None)
    return action


def main():
    parser = argparse.ArgumentParser(description="Riko as a floating desktop avatar.")
    parser.add_argument("--asr", choices=["remote", "local", "groq"], help="speech to text (overrides the config)")
    parser.add_argument("--tts", choices=["hub", "none", "openai", "sovits", "fish"], help="her voice (overrides the config)")
    args = parser.parse_args()

    # Line-buffer stdout even when piped to a log file, so tail -f shows things as they happen.
    sys.stdout.reconfigure(line_buffering=True)
    import faulthandler
    # A native crash (PortAudio used to SIGSEGV here when the L key closed
    # the mic stream from the Qt thread mid-read) dies with no Python
    # traceback: this makes even a SIGSEGV/SIGABRT print where it happened.
    faulthandler.enable(file=sys.stderr, all_threads=True)
    faulthandler.register(signal.SIGUSR1)  # kill -USR1 -> python backtrace (hang debugging)

    def _print_exc(etype, value, tb):
        """Print an uncaught exception and KEEP RUNNING: a Qt slot or worker
        thread raising must never be a second 'the app just closes'."""
        traceback.print_exception(etype, value, tb)
        sys.stderr.flush()

    sys.excepthook = _print_exc

    def _print_thread_exc(args):
        print(f"[thread] uncaught exception in {args.thread.name!r}:",
              file=sys.stderr, flush=True)
        _print_exc(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _print_thread_exc

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
    runtime, browser, sense = client_tools.start_desktop(cfg, hub_client.get())
    if sense is not None and not sense.awareness.enabled:
        print('[desktop] awareness off — tick "Let her see my desktop" in the '
              "tray to let her see windows/screens and switch workspaces",
              flush=True)
    # Typing + dictation (docs/TYPING.md): ydotool keystroke tools plus the
    # dictation mode that types her speech into a pinned window. Registered
    # through client_tools.register_extra, so it appears with the awareness
    # toggle and vanishes with it. Needs both sense and the client runtime.
    typing_svc = None
    if sense is not None and runtime is not None:
        try:
            from riko import typing as typing_pack
            typing_svc = typing_pack.install(sense, cfg=cfg, bridge=browser,
                                             runtime=runtime)
            session.dictation_filter = typing_svc.route
        except Exception as e:  # noqa: BLE001 — typing off must not kill the app
            print(f"[desktop] typing/dictation unavailable ({e})", flush=True)
    # App editing (docs/TYPING.md): Obsidian Local REST API + Kate tools.
    # Registered through the same client_tools.register_extra hook, so
    # obsidian_* / kate_* / find_file ride the awareness gate too. The
    # diff approvals run through the client-side confirmation path
    # (riko/appedit raises client_tools.NeedConfirmation).
    if sense is not None and runtime is not None:
        try:
            from riko import appedit
            appedit.install(sense, runtime=runtime, bridge=browser)
        except Exception as e:  # noqa: BLE001 — app edit off must not kill the app
            print(f"[desktop] app-editing tools unavailable ({e})", flush=True)
    hub_client.start_state_poller()  # notice switches made from the web page or another PC
    session.follow_hub_events()  # hub-fired timer replies reach her like normal turns (docs/TIMERS.md D)
    session.greet_on_start()  # she says hello once when the page is ready (desktop.greet_on_start)
    active = hub_client.active_persona() or {}
    print(f"\n=== Riko desktop  |  hub: {hub.url}  |  persona: {active.get('name', '?')}"
          f"  |  ears: {cfg['asr']}  |  voice: {cfg['tts']} ===")

    QGuiApplication.setDesktopFileName(APP_ID)  # becomes the Wayland app_id
    # Roam strip (docs/WALK_MEMORY.md W): the shaped click-through method that
    # works today is XWayland, so under Wayland we run the xcb platform unless
    # the user forced a platform themselves.
    _roam_wanted = str(((cfg.get("desktop") or {}).get("roam") or "off")).lower() == "strip"
    _roam_backend = str(((cfg.get("desktop") or {}).get("roam_backend") or "auto")).lower()
    if _roam_wanted and _roam_backend in ("auto", "xcb") and os.environ.get("WAYLAND_DISPLAY"):
        cur = os.environ.get("QT_QPA_PLATFORM")
        # A session-wide "wayland" default must not defeat the strip: shaped
        # click-through only exists on the X side. Honour a deliberate choice
        # (xcb, or e.g. vnc/offscreen for CI) but upgrade "wayland"/unset to xcb.
        if cur in (None, "", "wayland"):
            os.environ["QT_QPA_PLATFORM"] = "xcb"
            print(f"[roam] forcing QT_QPA_PLATFORM=xcb (XWayland strip with shaped input; was {cur!r})",
                  flush=True)
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_ID)
    app.setQuitOnLastWindowClosed(False)  # hiding the window keeps her running in the tray
    signal.signal(signal.SIGINT, lambda *_: app.quit())  # Ctrl+C in the terminal
    keepalive = QTimer()  # lets Python see Ctrl+C while Qt's loop runs
    keepalive.start(250)
    keepalive.timeout.connect(lambda: None)

    # Ask the compositor to float/size/pin her before the window maps (Hyprland only;
    # mapping tiled and resizing afterwards leaves QtWebEngine's surface short).
    # In strip mode the window is override-redirect (unmanaged), so rules don't apply.
    # The size is the base window x the persona's saved scroll-to-size, so the
    # scroll = window-size feature starts from where the owner left off.
    d = cfg.get("desktop") or {}
    base_w, base_h = int(d.get("width", 420)), int(d.get("height", 680))
    pid0 = (hub_client.active_persona() or {}).get("id") or "default"
    scale0 = avatar_scale.clamp_scale(appstate.load_scale(pid0) or d.get("avatar_scale", 1.0))
    win_w, win_h = round(base_w * scale0), round(base_h * scale0)
    ruled = False if _roam_wanted else _hyprland_rule(win_w, win_h)
    win = AvatarWindow(cfg, scale=scale0)
    win.on_focus = hub_client.note_chat_read  # focus = her chat was seen (docs/TIMERS.md D)
    # Approvals answered by a physical key in her window resolve on key
    # RELEASE (the injected Enter must not collide with the held approval
    # Enter on the Wayland seat — see make_approval_key_handler).
    win.approval_keys = make_approval_key_handler(win, runtime)
    bridge = Bridge()
    roamc = RoamController(win, cfg, scale=scale0)
    win.roam_strip = False
    roamc.filter = _NativeFilter(roamc)   # RoamController keeps it alive: Qt does not own it
    app.installNativeEventFilter(roamc.filter)

    def show_window():
        if roamc.enabled and not roamc.in_strip:
            roamc.enter_strip()      # strip flags + size + bottom-edge placement
            if roamc.in_strip:
                win.roam_strip = True
                win.activateWindow()
                hub_client.note_chat_read()  # the owner sees her chat (docs/TIMERS.md D)
                return
        win.show()
        win.raise_()
        win.activateWindow()
        hub_client.note_chat_read()  # the owner sees her chat (docs/TIMERS.md D)

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
        elif cmd.startswith("listen-ui:"):  # reflect a mode change on the tray
            always = cmd.partition(":")[2] == "always"
            listen_action.setChecked(always)
            tray.setToolTip("Riko — always listening (L switches back)" if always else "Riko")
            # the name gate only bites in always mode: re-tick + re-enable
            sync_name_gate_action(name_gate_action, session)
        elif cmd.startswith("namegate-ui:"):  # the name-gate tick changed
            # (tray click, riko-ctl or a control phrase): same Qt thread rule
            sync_name_gate_action(name_gate_action, session)
        elif cmd.startswith("awareness-ui:"):  # reflect an awareness toggle
            # made by the set_desktop_awareness tool (worker thread) onto the
            # Qt thread, exactly like listen-ui above
            sense_action.setChecked(cmd.partition(":")[2] == "on")
        elif cmd.startswith("dictation-ui:"):  # reflect a dictation toggle
            # made by the set_dictation tool, riko-ctl or the speech router
            on = cmd.partition(":")[2] == "on"
            dictation_action.setChecked(on)
            tray.setToolTip("Riko — dictating" if on else "Riko")
        elif cmd == "reset-size":  # tray Reset size: the page resets scale +
            # camera and POSTs scale 1.0 back through /control/avatar-scale
            win.js("window.riko && riko.resetSize && riko.resetSize()")
        elif cmd.startswith("avatar-scale:"):  # scroll = zoom her (see below)
            parts = cmd.split(":")             # final:window:gesture-flag
            try:
                request_avatar_scale(float(parts[1]), float(parts[2]), parts[3] == "1")
            except (ValueError, IndexError):
                pass

    bridge.command.connect(on_command, Qt.ConnectionType.QueuedConnection)

    # ── Scroll = zoom her ────────────────────────────────────────────────
    # The PAGE renders the zoom every frame (client/js/scale.js): the wheel
    # glides her VISUAL scale in-page, and the real window only moves at
    # gesture boundaries — ONE headroom grow on the first wheel event
    # ({gesture: true, window: target x 1.6}) and ONE fit once the wheel
    # has been quiet 400 ms ({gesture: false, window: final}); the page
    # re-frames its camera the moment the canvas size lands, so both window
    # moves are invisible (pixels-per-metre + feet pixels unchanged).
    # /control/avatar-scale carries {scale, window, gesture}.
    # Under Hyprland Wayland clients cannot size or place themselves, so
    # resize AND move go through the compositor for HER OWN window only, as
    # ONE batched hyprctl request over CACHED window + monitor state
    # (riko/avatar_scale.HyprlandSizer; Lua hl.dsp.window.* selected by
    # address, legacy hyprlang by class — syntax picked with
    # riko/desktop_sense's config-style detection, v0.56.2 batch semantics
    # source-verified). That IO is plain subprocesses, so the HTTP thread
    # runs it inline and the POST's JSON answers {window, max}: the page
    # keeps its headroom and its fit clamp honest from those numbers. Qt
    # paths (X11 resize/move, the roam strip's band) hop to the Qt thread
    # through the bridge and COALESCE on a trailing timer; GNOME may refuse
    # the move — the resize still happens. Clamping to the monitor lives in
    # the pure math: past the fit limit the size stops growing, nothing
    # else changes.
    scale_state = {"want": None, "applied": None, "cur": scale0}
    scale_sizer = (avatar_scale.HyprlandSizer(base_w, base_h, APP_ID)
                   if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl")
                   else None)
    scale_timer = QTimer()
    scale_timer.setSingleShot(True)
    scale_timer.setInterval(120)

    def apply_avatar_scale(scale, window_scale=None, persist=True):
        """Qt thread: put the window (or the roam strip) at the request.
        `scale` is her final size (what persists); `window_scale` is what
        the canvas needs right now — headroom above `scale` mid-gesture,
        == scale at the fit."""
        s = avatar_scale.clamp_scale(scale)
        ws = avatar_scale.clamp_window_scale(
            scale if window_scale is None else window_scale)
        scale_state["cur"] = s
        if roamc.in_strip:
            roamc.set_scale(avatar_scale.clamp_scale(ws))  # band follows, bottom anchored
        else:
            scr = win.screen() or QGuiApplication.primaryScreen()
            g = scr.availableGeometry()
            x, y, w, h = avatar_scale.scaled_geometry(
                (g.x(), g.y(), g.width(), g.height()),
                (win.x(), win.y(), win.width(), win.height()), base_w, base_h,
                ws, max_scale=avatar_scale.WINDOW_SCALE_MAX)
            win.resize(w, h)
            win.move(x, y)   # grow upward from the feet where the WM allows it
        if persist:
            pid = (hub_client.active_persona() or {}).get("id") or "default"
            appstate.save_scale(pid, s)

    def request_avatar_scale(scale, window_scale=None, gesture=False):
        """Newest request (bridge thread == Qt thread here). Grow requests
        and the final fit land through the same coalescer; the trailing
        timer always applies the newest, so the fit can never be lost."""
        scale_state["want"] = (round(float(scale), 4),
                               round(float(scale if window_scale is None else window_scale), 4),
                               bool(gesture))
        if not scale_timer.isActive():
            _scale_apply_want()
        scale_timer.start()   # trailing catch-up for anything newer

    def _scale_apply_want():
        if scale_state["want"] != scale_state["applied"]:
            s, ws, gesture = scale_state["want"]
            apply_avatar_scale(s, ws, persist=not gesture)
            scale_state["applied"] = scale_state["want"]

    def _scale_timer_fire():
        _scale_apply_want()
        if scale_state["want"] != scale_state["applied"]:
            scale_timer.start()   # newer still? keep catching up
    scale_timer.timeout.connect(_scale_timer_fire)
    roamc.on_leave = lambda: apply_avatar_scale(scale_state["cur"], persist=False)

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
            return {"ok": True, "recording": session.recorder.recording,
                    "visible": win.isVisible(), "listen_mode": session.listen_mode,
                    "name_gate": session.name_gate,
                    "avatar_scale": scale_state["cur"],
                    "dictation": typing_svc.dictation.status() if typing_svc else None}
        elif cmd == "name-gate":
            # riko-ctl name-gate on|off (and the `riko-ctl listen name-gate`
            # spelling): always-listen only — the gate never bites
            # push-to-talk, but the setting persists for when always returns
            want = str(args.get("enabled", "")).strip().lower()
            if want in ("on", "true", "1", "yes"):
                session.set_name_gate(True)
            elif want in ("off", "false", "0", "no"):
                session.set_name_gate(False)
            elif want:
                return {"ok": False, "error": "name-gate: on | off"}
            bridge.command.emit(f"namegate-ui:{'on' if session.name_gate else 'off'}")
            return {"ok": True, "name_gate": session.name_gate,
                    "listen_mode": session.listen_mode}
        elif cmd == "listen-mode":
            want = str(args.get("mode", "")).strip().lower()
            if want:
                if want not in ("push_to_talk", "always"):
                    return {"ok": False, "error": "listen-mode: push_to_talk | always"}
                session.set_listen_mode(want)
            else:
                session.toggle_listen_mode()
            return {"ok": True, "listen_mode": session.listen_mode}
        elif cmd in ("approve", "deny"):
            if runtime is None:
                return {"ok": False, "error": "client tools are off (desktop.client.enabled: false)"}
            return runtime.approve_hook() if cmd == "approve" else runtime.deny_hook()
        elif cmd == "approval-pending":
            return {"ok": True, "pending": runtime.pending_approval() if runtime else None}
        elif cmd in ("desktop-sense", "desktop-awareness"):
            # docs/DESKTOP_SENSE.md: the privacy master toggle (the same
            # single path as the tray checkbox and the set_desktop_awareness
            # tool: flip + persist + re-register live + tray tick;
            # riko-ctl desktop-sense [on|off])
            if sense is None:
                return {"ok": False, "error": "desktop awareness is unavailable on this PC"}
            want = str(args.get("enabled", "")).strip().lower()
            if want in ("on", "true", "1", "yes"):
                return {"ok": True, "enabled": _awareness_apply(True)}
            elif want in ("off", "false", "0", "no"):
                return {"ok": True, "enabled": _awareness_apply(False)}
            elif want:
                return {"ok": False, "error": "desktop-sense: on | off"}
            return {"ok": True, "enabled": sense.awareness.enabled}
        elif cmd in ("dictation", "dictation-mode"):
            # docs/TYPING.md §8: riko-ctl dictation on|off (the same path as
            # the tray checkbox; the pin is whatever window is focused now)
            if typing_svc is None:
                return {"ok": False, "error": "dictation needs desktop "
                         "awareness + client tools (desktop awareness off?)"}
            want = str(args.get("enabled", "")).strip().lower()
            try:
                if want in ("on", "true", "1", "yes"):
                    _dictation_apply(True)
                elif want in ("off", "false", "0", "no"):
                    _dictation_apply(False)
                elif want:
                    return {"ok": False, "error": "dictation: on | off"}
            except Exception as e:  # ToolError etc. from the engine
                return {"ok": False, "error": str(e)}
            bridge.command.emit(f"dictation-ui:{'on' if typing_svc.dictation.on else 'off'}")
            return {"ok": True, **typing_svc.dictation.status()}
        elif cmd == "stop-typing":
            if typing_svc is None:
                return {"ok": False, "error": "typing tools are off"}
            return {"ok": True, "note": typing_svc.engine.stop()}
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
        elif cmd == "obsidian-key":
            # docs/TYPING.md: the Obsidian Local REST API key lands in
            # ~/.config/riko/obsidian.json (0600) — tray "Obsidian…" or
            # `riko-ctl obsidian-key [key]`. Never echoed back, never to the hub.
            from riko.appedit import obsidian as _ob
            key = str(args.get("key", "")).strip()
            if key:
                try:
                    _ob.save_key(key)
                except Exception as e:      # ToolError on an empty/blank key
                    return {"ok": False, "error": str(e)}
                return {"ok": True, "set": True}
            return {"ok": True, "set": _ob.key_is_set()}
        elif cmd == "history-import":
            msgs = args.get("messages")
            if not isinstance(msgs, list) or not msgs:
                return {"ok": False, "error": 'history-import: send {"messages": [{"role","content"}, ...]}'}
            try:
                n = hub_client.get().import_history(msgs)
            except hub_client.HubError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "imported": n}
        elif cmd == "body-capabilities":
            # the avatar page reports the gesture library it can play right
            # now (built-ins + .vrma drop-ins, docs/BODY.md §1/§5)
            if runtime is None:
                return {"ok": False, "error": "client tools are off (desktop.client.enabled: false)"}
            return runtime.set_body(args)
        elif cmd == "walk-state":
            # roam strip (docs/WALK_MEMORY.md W): remember where she stopped
            x = args.get("x")
            if x is not None and not roam_state.save_x(x):
                return {"ok": False, "error": f"bad walk-state x: {x!r}"}
            return {"ok": True}
        elif cmd == "bbox":
            # her projected body box (page coords = window coords), ~10 Hz:
            # desktop.py shapes the click-through input region from this
            try:
                roamc.update_bbox(args["x"], args["y"], args["w"], args["h"])
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "error": "bbox: need numeric x, y, w, h"}
            return {"ok": True}
        elif cmd == "avatar-scale":
            # scroll over her = her zoom: the PAGE renders the visual zoom
            # every frame and asks the desktop only for WINDOW moves at
            # gesture boundaries — {scale: final size (persisted at the
            # fit), window: the scale the canvas needs (headroom during the
            # gesture, == scale at the fit), gesture: bool}. Numbers only;
            # the band clamps live in riko/avatar_scale so page and desktop
            # can't drift apart. The reply carries the window scale really
            # applied and the monitor-fit ceiling for final scales.
            try:
                s = float(args.get("scale"))
                w = float(args.get("window", args.get("scale")))
            except (TypeError, ValueError):    # missing, None or text
                return {"ok": False, "error": "avatar-scale: numeric scale (0.4 - 2.5)"}
            if s != s or s in (float("inf"), float("-inf")) \
                    or w != w or w in (float("inf"), float("-inf")):
                return {"ok": False, "error": "avatar-scale: numeric scale (0.4 - 2.5)"}
            s = avatar_scale.clamp_scale(s)
            w = avatar_scale.clamp_window_scale(w)
            gesture = bool(args.get("gesture"))
            if scale_sizer is not None and not roamc.in_strip:
                # Wayland cannot self-size: compositor IO is subprocesses
                # only, fine on this thread; one batched hyprctl per apply
                # over the sizer's cached window + monitor state.
                res = scale_sizer.apply(w)
                if res.get("ok") and not gesture:
                    scale_state["cur"] = s
                    pid = (hub_client.active_persona() or {}).get("id") or "default"
                    appstate.save_scale(pid, s)
                return {"ok": bool(res.get("ok")), "scale": s,
                        "window": res.get("scale", w),
                        "max": res.get("max", avatar_scale.SCALE_MAX),
                        "error": res.get("error", "")}
            bridge.command.emit(f"avatar-scale:{s:.4f}:{w:.4f}:{'1' if gesture else '0'}")
            if not gesture:
                scale_state["cur"] = s  # the Qt apply lands momentarily
            return {"ok": True, "scale": s, "window": w,
                    "max": avatar_scale.SCALE_MAX}
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
    # Exclusive check marks + triggered(checked)-safe id binding: see
    # build_persona_menu (owner bug: switching from the tray did nothing).
    build_persona_menu(menu, session)

    # Always listen: hands-free VAD ears with no listening pill, vs today's
    # hold-Space push-to-talk. Checkable; the check + tooltip also follow
    # toggles made by the L shortcut, riko-ctl or the page (listen-ui event).
    listen_action = QAction(f"Always listen{hint('listen_mode')}", menu)
    listen_action.setCheckable(True)
    listen_action.setToolTip("hands-free ears: no pill while she hears you; "
                             "pauses while she speaks")
    listen_action.triggered.connect(lambda checked=None: session.toggle_listen_mode())

    # Name gate: with always-listen on, ignore utterances that don't say the
    # active persona's name (its hub name/keywords plus listen.name_aliases).
    # Lives right under the ears switch; greyed out in push-to-talk.
    name_gate_action = build_name_gate_action(menu, session)

    # Desktop awareness (docs/DESKTOP_SENSE.md): "Let her see my desktop" —
    # the privacy master toggle. Off (the default) she only has the
    # awareness status/toggle tools; any toggle (tray, tool, riko-ctl) goes
    # through runtime.awareness_toggle, which re-registers the catalogue with
    # the hub right away — no restart.
    def _awareness_apply(enabled):
        """Single toggle path for tray/tool/ctl; returns the applied state."""
        if runtime is not None and getattr(runtime, "awareness_toggle", None):
            return bool(runtime.awareness_toggle(bool(enabled)))
        if sense is None:
            return False
        new = sense.awareness.set(bool(enabled))  # client tools off: persist only
        print(f"[desktop] desktop awareness {'on' if new else 'off'}", flush=True)
        return new

    sense_action = build_sense_action(menu, sense, _awareness_apply)
    if runtime is not None:
        # tool/ctl toggles run on worker threads: the tick lands on the Qt
        # thread through the same queued bridge as listen-ui
        runtime.awareness_tray = lambda new: bridge.command.emit(
            f"awareness-ui:{'on' if new else 'off'}")

    # Dictation (docs/TYPING.md §8): the tray checkbox + 'Stop typing'.
    # The mode itself lives in riko/typing; the checkbox follows toggles
    # made by the tool / riko-ctl / control phrases through dictation-ui.
    def _dictation_apply(enabled):
        """Single dictation toggle path (tray/ctl): returns applied state."""
        if typing_svc is None:
            return False
        (typing_svc.dictation.start() if enabled
         else typing_svc.dictation.stop())
        return bool(typing_svc.dictation.on)

    def _dictation_notice(msg):
        """The small local notice beside her (no hub turn): a HUD line +
        terminal + honest checkbox/tooltip state."""
        print(f"[dictation] {msg}", flush=True)
        viewer.emit("heard", text=f"🎤 {msg}")
        if typing_svc is not None:
            bridge.command.emit(
                f"dictation-ui:{'on' if typing_svc.dictation.on else 'off'}")

    if typing_svc is not None:
        typing_svc.dictation.notice = _dictation_notice
    dictation_action = build_dictation_action(menu, typing_svc, _dictation_apply)
    stop_typing_action = build_stop_typing_action(menu, typing_svc)

    for label, fn in ((f"Type to her{hint('type')}", lambda: on_command("type")),
                      (f"Talk / stop talking{hint('talk', 'hold ')}", session.talk_toggle)):
        action = QAction(label, menu)
        action.triggered.connect(fn)
        menu.addAction(action)
    menu.addAction(listen_action)
    menu.addAction(name_gate_action)
    menu.addAction(sense_action)
    menu.addAction(dictation_action)
    menu.addAction(stop_typing_action)
    for label, fn in ((f"Show / hide{hint('hide')}", toggle_window),
                      (f"Forget the chat{hint('forget')}", session.forget),
                      ("Reset size", lambda checked=None: on_command("reset-size")),
                      (f"Quit{hint('quit')}", app.quit)):
        action = QAction(label, menu)
        if label == "Reset size":
            action.setToolTip("back to normal size (plain scroll) and normal "
                              "camera (Ctrl+scroll); double-clicking her does "
                              "the same")
        action.triggered.connect(fn)
        menu.addAction(action)
    listen_action.setChecked(session.listen_mode == "always")
    if sense is not None:
        sense_action.setChecked(sense.awareness.enabled)
    if session.listen_mode == "always":
        tray.setToolTip("Riko — always listening (L switches back)")
    # keep the tray honest when the mode changes elsewhere (L key, riko-ctl)
    session.on_listen_mode = lambda mode: bridge.command.emit(f"listen-ui:{mode}")
    session.on_name_gate = lambda on: bridge.command.emit(
        f"namegate-ui:{'on' if on else 'off'}")

    # A timer that fired for ANOTHER persona: one quiet tray line, no switch
    # (docs/TIMERS.md D). Active-persona timers are spoken, not shown here.
    def timer_notice(persona_name, label):
        line = f"⏰ {persona_name}: {label or 'timer'}"
        if tray.isVisible():
            tray.showMessage("Riko", line, 8000)
        else:
            print(f"[timer] {line}")
    session.on_timer_notice = timer_notice

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

    # Obsidian (docs/TYPING.md): paste the Local REST API plugin's key into
    # a password dialog -> ~/.config/riko/obsidian.json (0600). The value
    # is never printed; only 'saved / cleared?' is mirrored in the tray.
    def obsidian_dialog():
        from riko.appedit import obsidian as _ob
        have = "set" if _ob.key_is_set() else "not set yet"
        key, ok = QInputDialog.getText(
            None, "Obsidian API key",
            f"Local REST API key (currently: {have}).\n"
            "Obsidian → Settings → Community plugins → Local REST API → "
            "API key.", QLineEdit.EchoMode.Password)
        if not ok:
            return
        key = str(key or "").strip()
        if not key:
            tray.showMessage("Riko", f"Obsidian key is {have} — type it to save.")
            return
        try:
            _ob.save_key(key)
            tray.showMessage("Riko", "Obsidian key saved "
                                     "(~/.config/riko/obsidian.json)")
        except Exception as e:
            tray.showMessage("Riko", f"Couldn't save the Obsidian key: {e}")

    obsidian_action = QAction("Obsidian…", menu)
    obsidian_action.triggered.connect(obsidian_dialog)
    menu.addAction(obsidian_action)
    tray.setContextMenu(menu)

    show_window()
    roamc.tray = tray
    if not _roam_wanted:
        QTimer.singleShot(400, lambda: _hyprland_setup(win_w, win_h, ruled))

    code = app.exec()
    if session.recorder.recording:
        session.recorder.stop()
    session.close()  # releases the always-listen mic stream if it is open
    if runtime is not None:
        runtime.stop()  # unregister from the hub's client channel
    print("Bye!")
    sys.exit(code)


if __name__ == "__main__":
    main()
