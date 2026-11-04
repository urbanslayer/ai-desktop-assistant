#!/usr/bin/env python3
"""Nested-headless-sway proof for the roam strip (docs/WALK_MEMORY.md W).

Runs entirely INSIDE a private nested sway (WLR_BACKENDS=headless, its own
XDG_RUNTIME_DIR + WAYLAND_DISPLAY; xwayland enabled) so nothing ever touches
the owner's Hyprland session:

  1. FakeHub (tests/conftest) answers hub calls; tts: none; no chat happens.
  2. A second X11 listener window (override-redirect) sits UNDER the strip,
     bottom-left: a click that lands on it proves click-through.
  3. desktop.py (RIKO_CONFIG points at a temp config, roam: strip) comes up
     as the strip; we assert bottom-edge placement + strip width from
     `swaymsg -t get_tree` (the compositor's own numbers), not from Qt logs.
  4. XTEST (xcb_test_fake_input) clicks: an EMPTY strip point must reach the
     listener (and NOT the page), a point on HER body must reach the page
     (its pointerdown console line shows up in desktop.py's stdout) and not
     the listener.
  5. grim screenshots (from inside the nested session) land in --outdir.

Everything started here is killed on exit (only our own PIDs).

Usage:  uv run python client/tools/roam_strip_smoke.py [--outdir DIR]
"""
import argparse
import ctypes
import ctypes.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TMP_DEFAULT = os.environ.get("TMPDIR", str(Path.home() / ".hermes/cache/scratch"))
SCREEN_W, SCREEN_H = 1920, 1080

# ── tiny xcb helpers (listener window + XTEST clicks) ────────────────────

class Screen(ctypes.Structure):
    # xcb_screen_t (from xcb_get_setup roots), NOT the Xlib Screen:
    # root, default_visual, widths, heights, depth/visual list…
    _fields_ = [("root", ctypes.c_uint32), ("default_visual", ctypes.c_uint32),
                ("width_in_pixels", ctypes.c_uint32), ("height_in_pixels", ctypes.c_uint32),
                ("width_in_millimeters", ctypes.c_uint32),
                ("height_in_millimeters", ctypes.c_uint32),
                ("root_depth", ctypes.c_uint8), ("backing_store", ctypes.c_uint8),
                ("visuals_len", ctypes.c_uint8), ("pad1", ctypes.c_uint8)]


class Iterator(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("rem", ctypes.c_uint32)]


class XConn:
    """One xcb connection: makes the listener window and fakes clicks with xtest."""

    def __init__(self):
        lib = ctypes.util.find_library("xcb")
        if not lib:
            raise RuntimeError("libxcb not found")
        self.lib = ctypes.CDLL(lib)
        lib = self.lib
        lib.xcb_connect.restype = ctypes.c_void_p
        lib.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        lib.xcb_connection_has_error.argtypes = [ctypes.c_void_p]
        lib.xcb_get_setup.restype = ctypes.c_void_p
        lib.xcb_get_setup.argtypes = [ctypes.c_void_p]
        lib.xcb_setup_roots_iterator.restype = Iterator
        lib.xcb_setup_roots_iterator.argtypes = [ctypes.c_void_p]
        lib.xcb_create_window.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32,
                                          ctypes.c_uint32, ctypes.c_int16, ctypes.c_int16,
                                          ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint16,
                                          ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32,
                                          ctypes.c_void_p]
        lib.xcb_configure_window.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_uint32, ctypes.c_void_p]
        lib.xcb_map_window.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.xcb_flush.argtypes = [ctypes.c_void_p]
        lib.xcb_generate_id.argtypes = [ctypes.c_void_p]
        lib.xcb_generate_id.restype = ctypes.c_uint32
        lib.xcb_poll_for_event.argtypes = [ctypes.c_void_p]
        lib.xcb_poll_for_event.restype = ctypes.c_void_p
        self.conn = lib.xcb_connect(None, None)
        if not self.conn or lib.xcb_connection_has_error(self.conn):
            raise RuntimeError("xcb connect failed (is DISPLAY set to the nested XWayland?)")
        # NOTE: the ctypes cast of xcb_setup roots below is UNRELIABLE on
        # XWayland (wrong struct layout -> garbage root id; the first smoke
        # silently made its listener a child of a nonexistent window). The
        # real root comes from query_tree(strip_xid).parent once desktop.py
        # has mapped the strip; screen.root is only a fallback.
        it = lib.xcb_setup_roots_iterator(lib.xcb_get_setup(self.conn))
        self.screen = ctypes.cast(it.data, ctypes.POINTER(Screen)).contents
        self.root = self.screen.root
        # XTEST lives in its own lib, not libxcb
        self.xtest = ctypes.CDLL(ctypes.util.find_library("xcb-xtest") or "libxcb-xtest.so.0")

    def query_tree(self, wid):
        """(root, parent, children) per the X server."""
        lib = self.lib
        ct = ctypes

        class Cookie(ct.Structure):
            _fields_ = [("sequence", ct.c_uint32)]

        class TreeReply(ct.Structure):
            _fields_ = [("response_type", ct.c_uint8), ("pad", ct.c_uint8),
                        ("sequence", ct.c_uint16), ("length", ct.c_uint32),
                        ("root", ct.c_uint32), ("parent", ct.c_uint32),
                        ("n_children", ct.c_uint32), ("n_properties", ct.c_uint32),
                        ("format_", ct.c_uint8), ("pad2", ct.c_uint8 * 23)]

        if not hasattr(lib, "_tree_proto"):
            lib.xcb_query_tree.argtypes = [ct.c_void_p, ct.c_uint32]
            lib.xcb_query_tree.restype = Cookie
            lib.xcb_query_tree_reply.argtypes = [ct.c_void_p, Cookie, ct.c_void_p]
            lib.xcb_query_tree_reply.restype = ct.c_void_p
            lib._tree_proto = True
        ptr = lib.xcb_query_tree_reply(self.conn, lib.xcb_query_tree(self.conn, wid), None)
        if not ptr:
            return None
        r = ct.cast(ptr, ct.POINTER(TreeReply)).contents
        kids = [int.from_bytes(ct.string_at(ptr + 32 + i * 4, 4), "little")
                for i in range(r.n_children)]
        return r.root, r.parent, kids

    def make_listener(self, x, y, w, h):
        """Map an override-redirect window under the root (the "window below").

        No event mask is selected: a pointer-less XWayland (nested sway with
        WLR_LIBINPUT_NO_DEVICES) validates core EventMask button bits against
        the core pointer's button count and answers BadValue — the window
        would never exist if we asked for ButtonPress. Routing is asserted
        through ptr_state() (QueryPointer's child), not delivered events.
        """
        lib = self.lib
        wid = lib.xcb_generate_id(self.conn)
        vals = (ctypes.c_uint32 * 1)(1)   # XCB_CW_OVERRIDE_REDIRECT (1<<8) = True
        lib.xcb_create_window(self.conn, 0, ctypes.c_uint32(wid),
                              ctypes.c_uint32(self.root),
                              ctypes.c_int16(x), ctypes.c_int16(y),
                              ctypes.c_uint16(w), ctypes.c_uint16(h),
                              ctypes.c_uint16(0), 1, 0, 1 << 8, vals)
        lib.xcb_map_window(self.conn, ctypes.c_uint32(wid))
        lib.xcb_flush(self.conn)
        self.wid = wid
        return wid

    def stack_below(self, wid, sibling):
        """Restack wid directly below sibling (mapped-after-strip must go down)."""
        vals = (ctypes.c_uint32 * 2)(sibling, 1)   # sibling, stack_mode=Below
        self.lib.xcb_configure_window(self.conn, ctypes.c_uint32(wid),
                                      (1 << 5) | (1 << 6), vals)
        self.lib.xcb_flush(self.conn)

    def ptr_state(self):
        """(root_x, root_y, child): the X server's own pick — which window a
        click at the pointer WOULD hit. Honours shaped input regions, so it
        proves click-through without needing delivered button events."""
        lib = self.lib
        ct = ctypes

        class PtrCookie(ct.Structure):
            _fields_ = [("sequence", ct.c_uint32)]

        class PtrReply(ct.Structure):
            _fields_ = [("response_type", ct.c_uint8), ("same_screen", ct.c_uint8),
                        ("sequence", ct.c_uint16), ("length", ct.c_uint32),
                        ("root", ct.c_uint32), ("child", ct.c_uint32),
                        ("root_x", ct.c_int16), ("root_y", ct.c_int16),
                        ("child_x", ct.c_int16), ("child_y", ct.c_int16)]

        if not hasattr(lib, "_ptr_proto"):
            lib.xcb_query_pointer.argtypes = [ct.c_void_p, ct.c_uint32]
            lib.xcb_query_pointer.restype = PtrCookie
            lib.xcb_query_pointer_reply.argtypes = [ct.c_void_p, PtrCookie, ct.c_void_p]
            lib.xcb_query_pointer_reply.restype = ct.c_void_p
            lib._ptr_proto = True
        ptr = lib.xcb_query_pointer_reply(self.conn, lib.xcb_query_pointer(self.conn, self.root), None)
        if not ptr:
            return None
        r = ct.cast(ptr, ct.POINTER(PtrReply)).contents
        return (r.root_x, r.root_y, r.child)

    def input_rects(self, wid):
        """The window's INPUT shape rectangles per the server (empty shape = None,
        unshaped/full = [(0,0,w,h)])."""
        import ctypes.util as cu
        lib = self.lib
        ct = ctypes
        if not hasattr(self, "_shape"):
            self._shape = ct.CDLL(cu.find_library("xcb-shape") or "libxcb-shape.so.0")

        class Cookie(ct.Structure):
            _fields_ = [("sequence", ct.c_uint32)]

        class Rect(ct.Structure):
            _fields_ = [("x", ct.c_int16), ("y", ct.c_int16),
                        ("w", ct.c_uint16), ("h", ct.c_uint16)]

        class GetRectReply(ct.Structure):
            _fields_ = [("response_type", ct.c_uint8), ("pad", ct.c_uint8),
                        ("sequence", ct.c_uint16), ("length", ct.c_uint32),
                        ("n_rectangles", ct.c_uint32), ("format_", ct.c_uint8),
                        ("pad2", ct.c_uint8 * 15)]
        sh = self._shape
        if not hasattr(sh, "_proto"):
            sh.xcb_shape_query_version.argtypes = [ct.c_void_p, ct.c_uint8, ct.c_uint8]
            sh.xcb_shape_query_version.restype = Cookie
            sh.xcb_shape_query_version_reply.argtypes = [ct.c_void_p, Cookie, ct.c_void_p]
            sh.xcb_shape_query_version_reply.restype = ct.c_void_p
            sh.xcb_shape_get_rectangles.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_uint8]
            sh.xcb_shape_get_rectangles.restype = Cookie
            sh.xcb_shape_get_rectangles_reply.argtypes = [ct.c_void_p, Cookie, ct.c_void_p]
            sh.xcb_shape_get_rectangles_reply.restype = ct.c_void_p
            sh._proto = True
        sh.xcb_shape_query_version_reply(self.conn, sh.xcb_shape_query_version(self.conn, 1, 1), None)
        p = sh.xcb_shape_get_rectangles_reply(self.conn,
                                              sh.xcb_shape_get_rectangles(self.conn, wid, 2), None)
        if not p:
            return None
        r = ct.cast(p, ct.POINTER(GetRectReply)).contents
        return [(rr.x, rr.y, rr.w, rr.h) for rr in
                (ct.cast(p + 32 + i * 8, ct.POINTER(Rect)).contents
                 for i in range(r.n_rectangles))]

    def warp(self, x, y):
        """XTEST pointer warp. XWayland applies the emulated motion one event
        late, so send it twice and let the sprite settle before querying."""
        fake = self.xtest.xcb_test_fake_input
        fake.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int16,
                         ctypes.c_int16, ctypes.c_uint8]
        for _ in range(2):
            fake(self.conn, 6, 0, 0, self.root, x, y, 0)
            self.lib.xcb_flush(self.conn)
            time.sleep(0.08)
        time.sleep(0.15)

    def drain_errors(self):
        """Non-blocking: pop queued X errors (they ride the event queue)."""
        lib = self.lib
        out = []
        while True:
            ev = lib.xcb_poll_for_event(self.conn)
            if not ev:
                break
            b = ctypes.string_at(ev, 32)
            code = b[0] & 0x7F
            if code == 0:   # XCB_ERROR
                out.append(f"err={b[1]} bad={int.from_bytes(b[4:8], 'little'):#x}"
                           f" minor={int.from_bytes(b[8:10], 'little')}"
                           f" major={b[10]}")
        return out

    def get_geom(self, xid):
        """(x, y, w, h) of a window per the X server (root-relative)."""
        lib = self.lib
        ct = ctypes

        class Cookie(ct.Structure):
            _fields_ = [("sequence", ct.c_uint32)]

        class GeomReply(ct.Structure):
            # wire: type, depth, seq(2), len(4), root(4), x(2), y(2), w(2), h(2)
            # — the C reply has NO drawable field; the previous layout shifted
            # everything by 4 (returned (1920,648,0,0) for a 0,432 1920x648 win)
            _fields_ = [("response_type", ct.c_uint8), ("depth", ct.c_uint8),
                        ("sequence", ct.c_uint16), ("length", ct.c_uint32),
                        ("root", ct.c_uint32), ("x", ct.c_int16), ("y", ct.c_int16),
                        ("width", ct.c_uint16), ("height", ct.c_uint16),
                        ("border_width", ct.c_uint16), ("pad3", ct.c_uint8 * 2),
                        ("visual", ct.c_uint32)]

        if not hasattr(lib, "_geom_proto"):
            lib.xcb_get_geometry.argtypes = [ct.c_void_p, ct.c_uint32]
            lib.xcb_get_geometry.restype = Cookie
            lib.xcb_get_geometry_reply.argtypes = [ct.c_void_p, Cookie, ct.c_void_p]
            lib.xcb_get_geometry_reply.restype = ct.c_void_p
            lib._geom_proto = True
        ptr = lib.xcb_get_geometry_reply(self.conn, lib.xcb_get_geometry(self.conn, xid), None)
        if not ptr:
            return None
        r = ct.cast(ptr, ct.POINTER(GeomReply)).contents
        lib.xcb_free_reply = getattr(lib, "xcb_free_reply", None)
        return (r.x, r.y, r.width, r.height)

    def click(self, x, y):
        """XTEST: warp to (x, y) then press+release button 1 (root coords)."""
        self.warp(x, y)
        fake = self.xtest.xcb_test_fake_input
        fake(self.conn, 4, 1, 0, self.root, 0, 0, 0)   # ButtonPress
        self.lib.xcb_flush(self.conn)
        time.sleep(0.08)
        fake(self.conn, 5, 1, 0, self.root, 0, 0, 0)   # ButtonRelease
        self.lib.xcb_flush(self.conn)


# ── orchestration ─────────────────────────────────────────────────────────

def wait_for(patterns, log_lines, timeout, names):
    """Watch a list of stdout lines (appended by a reader thread) for regexes."""
    deadline = time.monotonic() + timeout
    found = {}
    while time.monotonic() < deadline and len(found) < len(patterns):
        for line in list(log_lines):
            for name, pat in zip(names, patterns):
                if name not in found and pat.search(line):
                    found[name] = line
        time.sleep(0.1)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outdir", default=str(Path(TMP_DEFAULT) / "riko-walk"))
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="riko-walk-sway-", dir=outdir))

    # ── owner-session hygiene ──
    base_env = dict(os.environ)
    for k in ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
        base_env.pop(k, None)
    # desktop.py's roam strip picks the click-through backend itself; a stray
    # QT_QPA_PLATFORM from our own shell would pin the platform and defeat it
    base_env.pop("QT_QPA_PLATFORM", None)
    xdg = work / "xdg"
    xdg.mkdir(mode=0o700)

    sway_cfg = work / "sway.config"
    sway_cfg.write_text(
        "output HEADLESS-1 resolution 1920x1080 position 0,0\n"
        "xwayland enabled\n"
        # sway exports DISPLAY (XWayland) to its children; capture it
        f'exec "sh -c \'sleep 1.5; echo $DISPLAY > {work}/display.env\'"\n'
        "seat * hide_cursor when-typing disable\n",
        encoding="utf-8")

    sway_env = dict(base_env, XDG_RUNTIME_DIR=str(xdg),
                    SWAYSOCK=str(work / "sway.sock"),
                    WLR_BACKENDS="headless", WLR_LIBINPUT_NO_DEVICES="1")
    sway_log = open(work / "sway.log", "wb")
    sway = subprocess.Popen(["/usr/bin/sway", "--config", str(sway_cfg)],
                            env=sway_env, stdout=sway_log, stderr=subprocess.STDOUT,
                            start_new_session=True)

    results = []  # (name, ok, detail)
    desktop = None
    try:
        # wait for the nested wayland socket
        wd = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and wd is None:
            for cand in sorted(xdg.glob("wayland-*")):
                if not cand.name.endswith(".lock"):
                    wd = cand.name
                    break
            time.sleep(0.2)
        if wd is None:
            raise RuntimeError("nested sway never created a wayland socket")
        wenv = dict(base_env, XDG_RUNTIME_DIR=str(xdg), WAYLAND_DISPLAY=wd,
                    SWAYSOCK=str(work / "sway.sock"))

        # XWayland DISPLAY: sway sets it in its own environment and hands it to
        # children — the config's `exec` line dumped it for us.
        display = ""
        deadline = time.monotonic() + 20
        denv_file = work / "display.env"
        while time.monotonic() < deadline and not display:
            if denv_file.exists():
                display = denv_file.read_text().strip()
            else:
                time.sleep(0.2)
        if not display or display == "\n":
            raise RuntimeError("no XWayland DISPLAY inside nested sway")
        print(f"[smoke] nested sway: {wd}, XWayland {display}", flush=True)
        wenv["DISPLAY"] = display
        os.environ["DISPLAY"] = display   # our own XConn (listener + XTEST) joins the nested XWayland

        # ── fake hub (tests/conftest FakeHub, in this process) ──
        sys.path.insert(0, str(REPO / "tests"))
        sys.path.insert(0, str(REPO))
        from conftest import FakeHub  # noqa: E402
        hub = FakeHub(api_key=None)
        print(f"[smoke] fake hub at {hub.url}", flush=True)

        # ── temp character_config for desktop.py (never the owner's keys) ──
        cfg_yaml = work / "character_config.yaml"
        cfg_yaml.write_text(f"""
hub:
  mode: remote
  url: {hub.url}
  api_key_env: RIKO_SMOKE_UNUSED_KEY
llm: {{base_url: http://127.0.0.1:1/v1, api_key_env: NOPE, model: auto}}
asr: remote
remote_asr: {{url: http://127.0.0.1:1, api_key_env: NOPE, model: x, language: en}}
local_asr: {{model: base.en, device: cpu}}
tts: none
desktop:
  width: 420
  height: 680
  bubble_seconds: 8
  greet_on_start: false
  roam: strip
  roam_backend: auto
  roam_speed: 1.0
  roam_every: [60, 240]
  roam_margin: 40
  client: {{enabled: false}}
""", encoding="utf-8")

        # ── the second (under-strip) client window: our X listener ──
        # created AFTER desktop.py maps the strip: the listener's parent must
        # be the real XWayland root (derived from the strip's window tree),
        # and being mapped last it is explicitly restacked below the strip.
        strip_h = min(680, int(SCREEN_H * 0.6))
        strip_y = SCREEN_H - strip_h

        # ── desktop.py under the nested compositor ──
        denv = dict(wenv, RIKO_CONFIG=str(cfg_yaml), RIKO_PORT="8137",
                    RIKO_ROAM_STATE=str(work / "roam_x.json"),
                    QTWEBENGINE_DISABLE_SANDBOX="1",
                    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --enable-unsafe-swiftshader --use-gl=angle --use-angle=swiftshader")
        dlog_path = work / "desktop.log"
        dlog = open(dlog_path, "wb")
        desktop = subprocess.Popen(["uv", "run", "python", "desktop.py", "--tts", "none"],
                                   cwd=str(REPO), env=denv, stdout=dlog,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        log_lines = []

        def reader():
            # tail -f semantics: a plain `for line in open(...)` sees EOF on
            # the (still empty) file and exits; poll for appended data instead
            carry = ""
            with open(dlog_path, "r", errors="replace") as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        time.sleep(0.2)
                        continue
                    carry += chunk
                    *lines, carry = carry.split("\n")
                    for line in lines:
                        line = line.rstrip()
                        log_lines.append(line)
                        print(f"  [desktop] {line}", flush=True)
        threading.Thread(target=reader, daemon=True).start()

        found = wait_for(
            [re.compile(r"\[roam\] strip ON \((\w+)\): (\d+)x(\d+) at \((-?\d+),(-?\d+)\)"),
             re.compile(r"\[roam\] input region -> \((\d+), (\d+), (\d+), (\d+)\)"),
             re.compile(r"input region ->")],  # first shaped bbox = page is live
            log_lines, 150, ["strip_on", "region", "_warm"])
        if "strip_on" not in found:
            raise RuntimeError("desktop.py never reported the strip (see desktop.log)")
        m = re.search(r"strip ON \((\w+)\): (\d+)x(\d+) at \((-?\d+),(-?\d+)\)",
                      found["strip_on"])
        backend, sw, sh, sx, sy = m[1], int(m[2]), int(m[3]), int(m[4]), int(m[5])
        print(f"[smoke] strip reported: {sw}x{sh} at ({sx},{sy}) via {backend}", flush=True)

        # ── compositor ground truth: sway's own geometry for the strip ──
        # (the compositor enforces placement here; Hyprland rules don't exist
        # in the nested session, so assert against sway's tree only)
        tree = json.loads(subprocess.run(["swaymsg", "-t", "get_tree"], env=wenv,
                                         capture_output=True, text=True,
                                         timeout=10).stdout)

        def walk(node):
            # tolerate any node shape: get_tree carries int window-id lists
            # under "focus" and occasional non-dict entries
            if isinstance(node, dict):
                yield node
                for k in ("nodes", "floating_nodes"):
                    kids = node.get(k)
                    if isinstance(kids, list):
                        for ch in kids:
                            yield from walk(ch)

        strip_xid = None
        for ln in log_lines:
            mx = re.search(r"enter_strip: place xid=([0-9a-f]+)", ln)
            if mx:
                strip_xid = int(mx[1], 16)

        listener = XConn()
        if strip_xid is not None:
            tq = listener.query_tree(strip_xid)
            if tq:
                listener.root = tq[1] or tq[0]   # parent of a top-level OR window = real root
        listener.make_listener(40, sy + 40, 500, 600)
        if strip_xid is not None:
            listener.stack_below(listener.wid, strip_xid)
        errs = listener.drain_errors()
        lg = listener.get_geom(listener.wid)
        print(f"[smoke] listener {listener.wid:#x} at {lg} under root "
              f"{listener.root:#x}, stacked below strip"
              + (f", X errors: {errs}" if errs else ""), flush=True)

        riko_views = []
        for n in walk(tree):
            if strip_xid is not None and n.get("window") == strip_xid:
                riko_views.append(n)   # exact: the X11 id desktop.py shaped
            elif ((n.get("app_id") or "") == "riko"
                  or (isinstance(n.get("window_properties"), dict)
                      and n["window_properties"].get("name") == "Riko")):
                riko_views.append(n)   # fallback: class/title match
        geo = None
        for v in riko_views:
            r = v.get("rect")
            if isinstance(r, list) and len(r) == 4 and r[2] >= SCREEN_W * 0.9:
                geo = r
                break
        src = "sway get_tree"
        if geo is None and strip_xid is not None:
            # override-redirect X windows may be absent from sway's tree;
            # the X server's own numbers are the next-best ground truth
            geo = listener.get_geom(strip_xid)
            src = f"x_get_geometry(xid={strip_xid:#x})"
        results.append(("strip placed at bottom edge",
                        bool(geo) and geo[1] == SCREEN_H - geo[3] and geo[0] == sx,
                        f"{src} rect {geo}"))
        results.append(("strip spans full screen width",
                        bool(geo) and geo[2] == SCREEN_W,
                        f"{src} rect {geo}"))

        shot1 = outdir / "sway-strip-on.png"
        subprocess.run(["grim", str(shot1)], env=wenv, timeout=15)
        print(f"[smoke] screenshot: {shot1}", flush=True)

        # ── click-through vs body ──
        if "region" not in found:
            raise RuntimeError("page never POSTED a bbox (input region never shaped)")
        # The first bbox can be a warm-up artifact; wait for a settled, body-
        # sized one (narrower than half the screen) and use the latest line.
        deadline = time.monotonic() + 60
        bx = by = bw = bh = None
        while time.monotonic() < deadline:
            regs = [ln for ln in log_lines if "input region ->" in ln]
            if regs:
                try:
                    bx, by, bw, bh = (int(v) for v in
                                      regs[-1].split("-> (")[1].rstrip(")").split(", "))
                    if bw < SCREEN_W * 0.5:
                        break
                except (ValueError, IndexError):
                    pass
            time.sleep(0.5)
        if not bw or bw >= SCREEN_W * 0.5:
            raise RuntimeError(f"bbox never settled to body size (last: {bx},{by} {bw}x{bh})")
        results.append(("input mask narrowed to her body box (not full strip)",
                        bw < SCREEN_W * 0.5 and bh <= sh,
                        f"region {bx},{by} {bw}x{bh} of strip {sw}x{sh}"))
        body_pt = (sx + bx + bw // 2, sy + by + bh // 2)
        # empty point: inside the strip, inside the listener window, clear of bbox
        empty_pt = (40 + 160, sy + sh - 90)
        assert not (bx <= empty_pt[0] - sx <= bx + bw), "empty point landed on her bbox"
        print(f"[smoke] body point {body_pt} (bbox {bx},{by} {bw}x{bh}); "
              f"empty point {empty_pt}", flush=True)

        # ── click routing through the shaped mask ──
        # A pointer-less XWayland rejects core ButtonPress event selection
        # (the strip's own Qt uses XInput2, so only the page sees clicks), so
        # the oracle is QueryPointer: the X server's own pick of the window a
        # click at the pointer would hit — shaped input regions included.
        # Ask the server directly what INPUT shape the strip carries: proof
        # the XFixes region actually landed (not just that desktop.py logged it).
        srv_rects = listener.input_rects(strip_xid)
        print(f"[smoke] strip INPUT rects per server: {srv_rects}", flush=True)
        results.append(("server-side INPUT shape equals the body box",
                        srv_rects == [(bx, by, bw, bh)],
                        f"server {srv_rects}, expected {[(bx, by, bw, bh)]}"))
        listener.warp(*empty_pt)
        st_e = listener.ptr_state()
        listener.warp(*body_pt)
        st_b = listener.ptr_state()
        print(f"[smoke] pointer over empty {empty_pt} -> {st_e}; "
              f"over body {body_pt} -> {st_b}", flush=True)
        results.append(("click in EMPTY strip area goes to the window BELOW (click-through)",
                        bool(st_e) and st_e[2] == listener.wid,
                        f"server pick {st_e}, listener={listener.wid:#x}"))
        results.append(("click ON HER BODY goes to the strip, not the window below",
                        bool(st_b) and strip_xid is not None and st_b[2] == strip_xid,
                        f"server pick {st_b}, strip={strip_xid:#x}" if strip_xid else f"server pick {st_b}"))

        # …and prove it with real XTEST clicks: only the body point may
        # produce a pointerdown on her page.
        def page_clicks():
            return sum("[page] pointerdown" in ln for ln in log_lines)
        n0 = page_clicks()
        listener.click(*empty_pt)
        time.sleep(0.6)
        n1 = page_clicks()
        listener.click(*body_pt)
        time.sleep(0.6)
        n2 = page_clicks()
        results.append(("real click on EMPTY point never reaches her page",
                        n1 == n0, f"page pointerdown {n0}->{n1}"))
        results.append(("real click on HER BODY reaches her page",
                        n2 > n1, f"page pointerdown {n1}->{n2}"))

        shot2 = outdir / "sway-click-on-her.png"
        subprocess.run(["grim", str(shot2)], env=wenv, timeout=15)
        print(f"[smoke] screenshot: {shot2}", flush=True)

    finally:
        for proc, name in ((desktop, "desktop.py"), (sway, "sway")):
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for proc, name in ((desktop, "desktop.py"), (sway, "sway")):
            if proc:
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        print(f"[smoke] killed what we started; logs kept in {work}", flush=True)

    print("\n=== nested-sway roam-strip results ===")
    ok_all = True
    for name, ok, detail in results:
        ok_all &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}   [{detail}]")
    print(f"desktop.log: {work / 'desktop.log'}")
    sys.exit(0 if ok_all and results else 1)


if __name__ == "__main__":
    main()
