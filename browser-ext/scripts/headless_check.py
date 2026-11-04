#!/usr/bin/env python3
"""REAL headless check of the Riko Helper extension (R2-F, manual).

    uv run python browser-ext/scripts/headless_check.py

Starts the desktop side (riko.client_tools.BrowserBridge) + a local test
page, then runs Firefox itself — headless, TEMPORARY profile (never yours),
extension temporarily installed by web-ext. On install the extension opens
its link tab (http://127.0.0.1:8766/link, served by the bridge; the tab's
content script link_relay.js owns the WebSocket — see the wss-upgrade note
in background.js). Then we drive browser_open + browser_read, one click that
must come back as needs_confirmation (the "Buy now" danger upgrade), and one
plain click.

Exits 0 on success, 1 on any failure, 3 if firefox/web-ext are missing or
the default port is busy (desktop running).
"""
import http.server
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from riko.client_tools import BrowserBridge  # noqa: E402

TOKEN = "headless-check-token"
PAGE = """<!doctype html><html><head><title>Riko headless test</title></head>
<body><h1>Riko headless test page</h1>
<p>Some readable text for the snapshot.</p>
<button onclick="this.textContent='clicked'">Buy now</button>
<a href="#top">a plain link</a>
</body></html>"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    firefox = shutil.which("firefox") or shutil.which("firefox-esr") or shutil.which("firefox-esp")
    npx = shutil.which("npx")
    if not firefox or not npx:
        print("SKIP: firefox and/or npx not found")
        return 3

    # The extension dials its default port and auto-pairs via GET /riko-pair,
    # so this check must own the default port.
    ws_port = 8766
    page_port = free_port()
    page_dir = Path(tempfile.mkdtemp(prefix="riko-testpage-"))
    (page_dir / "index.html").write_text(PAGE)
    import functools
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(page_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", page_port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{page_port}/index.html"

    profile = Path(tempfile.mkdtemp(prefix="riko-headless-profile-"))
    (profile / "user.js").write_text(
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
        'user_pref("dom.webnotifications.enabled", false);\n')

    bridge = BrowserBridge(TOKEN, "127.0.0.1", ws_port)
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="[ws] %(message)s")
    try:
        bridge.start()
    except RuntimeError as e:
        print(f"SKIP: {e} (is the desktop running?)")
        httpd.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(page_dir, ignore_errors=True)
        return 3
    log_path = Path(os.environ.get("TMPDIR", "/tmp")) / "riko-web-ext.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [npx, "--yes", "web-ext", "run", "--verbose", "--target", "firefox-desktop",
         "--firefox", firefox, "--firefox-profile", str(profile),
         "--no-reload", "--args=-headless"],
        cwd=REPO / "browser-ext", stdout=log, stderr=subprocess.STDOUT)

    ok = False
    try:
        is_helper = lambda: bridge.connected() and bridge.extension_name() == "Riko Helper"  # noqa: E731
        deadline = time.time() + 120
        while time.time() < deadline and not is_helper():
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if not is_helper():
            print("FAIL: the extension never connected (see", log_path, ")")
            return 1
        print(f"extension connected: {bridge.extension_name()} over ws://127.0.0.1:{ws_port}")

        opened = bridge.call("open", {"url": page_url, "new_tab": True})
        print("browser_open ->", opened)
        assert opened.get("ok") is True, "browser_open failed"

        snap = None
        resp = {}
        deadline = time.time() + 30
        while time.time() < deadline:
            resp = bridge.call("read", {})
            snap = resp.get("result") if isinstance(resp.get("result"), dict) else resp
            if (snap or {}).get("elements") or resp.get("ok") is False:
                break
            time.sleep(1.0)  # page/content script may still be settling
        print("browser_read raw ->", json.dumps(resp, ensure_ascii=False)[:400])
        if not snap or "title" not in snap:
            print("FAIL: browser_read did not return a snapshot")
            return 1
        print("browser_read ->", json.dumps({k: snap[k] for k in ("title", "url")}, ensure_ascii=False),
              "| text:", snap["text"][:60].replace("\n", " "),
              "| elements:", [(e["n"], e["tag"], e.get("text")) for e in snap["elements"]])
        assert snap["title"] == "Riko headless test", f"wrong title: {snap['title']!r}"
        assert any(e.get("text") == "Buy now" for e in snap["elements"]), "Buy button missing from snapshot"

        buy = next(e["n"] for e in snap["elements"] if e.get("text") == "Buy now")
        risky = bridge.call("click", {"n": buy})
        print("browser_click(Buy now) ->", risky)
        assert risky.get("needs_confirmation") is True, "the Buy click did not upgrade to danger"

        link = next(e["n"] for e in snap["elements"] if e.get("tag") == "a")
        clicked = bridge.call("click", {"n": link})
        print("browser_click(plain link) ->", clicked)
        assert clicked.get("ok") is True, "plain click failed"

        tabs = bridge.call("tabs", {})
        titles = [(t["n"], t["title"]) for t in tabs["result"]]
        print("browser_tabs ->", titles)
        assert not any("Riko Helper link" in (t[1] or "") for t in titles), \
            "the link tab leaked into browser_tabs numbering"
        ok = True
    finally:
        proc.send_signal(subprocess.SIGINT if hasattr(subprocess, "SIGINT") else 2)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["pkill", "-f", str(profile)], capture_output=True)
        bridge.stop()
        httpd.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(page_dir, ignore_errors=True)

    print("HEADLESS CHECK PASSED" if ok else "HEADLESS CHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
