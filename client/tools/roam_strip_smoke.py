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
    _fields_ = [("depth", ctypes.c_uint8), ("pad1", ctypes.c_uint8),
                ("root", ctypes.c_uint32), ("default_colormap", ctypes.c_uint32),
                ("white_pixel", ctypes.c_uint32), ("black_pixel", ctypes.c_uint32),
                ("current_input", ctypes.c_uint32), ("back_buffer", ctypes.c_uint32),
                ("width_in_pixels", ctypes.c_uint32), ("height_in_pixels", ctypes.c_uint32),
                ("width_in_millimeters", ctypes.c_uint32),
                ("height_in_millimeters", ctypes.c_uint32),
                ("min_instmaps", ctypes.c_uint32), ("installed_maps_len", ctypes.c_uint32)]


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
        lib.xcb_change_window_attributes.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
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
        it = lib.xcb_setup_roots_iterator(lib.xcb_get_setup(self.conn))
        self.screen = ctypes.cast(it.data, ctypes.POINTER(Screen)).contents
        # XTEST lives in its own lib, not libxcb
        self.xtest = ctypes.CDLL(ctypes.util.find_library("xcb-xtest") or "libxcb-xtest.so.0")

    def make_listener(self, x, y, w, h):
        """Map an override-redirect window that reports its button presses."""
        lib = self.lib
        wid = lib.xcb_generate_id(self.conn)
        # value list for create_window: one (mask_bit, value) pair: OverrideRedirect
        vals = (ctypes.c_uint32 * 1)(1)
        lib.xcb_create_window(self.conn, 0, ctypes.c_uint32(wid),
                              ctypes.c_uint32(self.screen.root),
                              ctypes.c_int16(x), ctypes.c_int16(y),
                              ctypes.c_uint16(w), ctypes.c_uint16(h),
                              ctypes.c_uint16(0), 1, 0, 1 << 11, vals)
        # XCB_EVENT_MASK_BUTTON_PRESS | BUTTON_RELEASE = 0x1004
        lib.xcb_change_window_attributes(self.conn, ctypes.c_uint32(wid),
                                         1 << 2, (ctypes.c_uint32 * 1)(0x1004))
        lib.xcb_map_window(self.conn, ctypes.c_uint32(wid))
        lib.xcb_flush(self.conn)
        self.wid = wid
        return wid

    def pump_clicks(self, seconds):
        """Drain events for a while; return list of (root_x, root_y) presses."""
        lib = self.lib
        lib.xcb_poll_for_event.restype = ctypes.c_void_p
        end = time.monotonic() + seconds
        out = []
        while time.monotonic() < end:
            ev = lib.xcb_poll_for_event(self.conn)
            if ev:
                b = ctypes.string_at(ev, 40)
                code = b[0] & 0x7F
                if code == 4:  # ButtonPress
                    rx = int.from_bytes(b[20:22], "little")
                    ry = int.from_bytes(b[22:24], "little")
                    out.append((rx, ry))
            else:
                time.sleep(0.02)
        return out

    def get_geom(self, xid):
        """(x, y, w, h) of a window per the X server (root-relative)."""
        lib = self.lib
        ct = ctypes

        class Cookie(ct.Structure):
            _fields_ = [("sequence", ct.c_uint32)]

        class GeomReply(ct.Structure):
            _fields_ = [("response_type", ct.c_uint8), ("pad", ct.c_uint8),
                        ("sequence", ct.c_uint32), ("length", ct.c_uint32),
                        ("root", ct.c_uint32), ("x", ct.c_int16), ("y", ct.c_int16),
                        ("width", ct.c_uint16), ("height", ct.c_uint16),
                        ("border_width", ct.c_uint16), ("depth", ct.c_uint8),
                        ("visual", ct.c_uint32)]  # pad3 + visual, generous tail

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
        lib = self.lib
        fake = self.xtest.xcb_test_fake_input
        fake.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int16,
                         ctypes.c_int16, ctypes.c_uint8]
        fake(self.conn, 6, 0, 0, self.screen.root, x, y, 0)   # MotionNotify
        lib.xcb_flush(self.conn)
        time.sleep(0.08)
        fake(self.conn, 4, 1, 0, self.screen.root, 0, 0, 0)   # ButtonPress
        lib.xcb_flush(self.conn)
        time.sleep(0.08)
        fake(self.conn, 5, 1, 0, self.screen.root, 0, 0, 0)   # ButtonRelease
        lib.xcb_flush(self.conn)


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
        strip_h = min(680, int(SCREEN_H * 0.6))
        strip_y = SCREEN_H - strip_h
        listener = XConn()
        listener.make_listener(40, strip_y + 40, 500, 600)
        print(f"[smoke] listener window mapped at (40,{strip_y + 40}) 500x600", flush=True)

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
        body_pt = (sx + bx + bw // 2, sy + by + bh // 2)
        # empty point: inside the strip, inside the listener window, clear of bbox
        empty_pt = (40 + 160, sy + sh - 90)
        assert not (bx <= empty_pt[0] - sx <= bx + bw), "empty point landed on her bbox"
        print(f"[smoke] body point {body_pt} (bbox {bx},{by} {bw}x{bh}); "
              f"empty point {empty_pt}", flush=True)

        listener.click(*empty_pt)
        got_empty = listener.pump_clicks(1.2)
        n_before = sum("[roam] forward pointerdown" in ln for ln in log_lines)
        listener.click(*body_pt)
        got_body = listener.pump_clicks(1.2)
        time.sleep(0.6)
        n_after = sum("[roam] forward pointerdown" in ln for ln in log_lines)

        results.append(("click in EMPTY strip area reaches window BELOW (click-through)",
                        any(abs(rx - empty_pt[0]) < 60 and abs(ry - empty_pt[1]) < 60
                            for rx, ry in got_empty),
                        f"listener got {got_empty}"))
        results.append(("click on HER BODY reaches the page, not the window below",
                        n_after > n_before and
                        not any(abs(rx - body_pt[0]) < 60 and abs(ry - body_pt[1]) < 60
                                for rx, ry in got_body),
                        f"page pointerdown {n_before}->{n_after}, listener {got_body}"))

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
