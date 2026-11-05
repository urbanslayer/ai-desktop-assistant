#!/usr/bin/env python3
"""REAL headless check of the Riko Helper extension (R2-F, manual).

    uv run python browser-ext/scripts/headless_check.py

Starts a local test page + a stub HTTP server on a SCRATCH port that answers
404 to /hello (the desktop is DOWN), then runs Firefox itself — headless,
TEMPORARY profile (never yours), extension temporarily installed by
web-ext. The scratch port + token are seeded straight into the profile's
extension storage file (legacy JSONFile backend, pinned via
ExtensionStorageIDB.enabled=false), so the check never shares the default
8766 with a live Riko desktop or the owner's running Firefox helper.
Phase 1 proves the probe gate: within the first seconds after startup the
extension must NOT open its link tab (no GET /link on the scratch port)
while /hello refuses us; only probes are allowed. Phase 2 kills the stub,
starts the real desktop side (riko.client_tools.BrowserBridge) on the same
scratch port, and waits for the supervisor's probe to succeed: the link tab
(http://127.0.0.1:<port>/link, served by the bridge) opens by itself and
the tab's content script link_relay.js brings the WebSocket up (the tab's
origin is http — see the wss-upgrade note in background.js). Then we drive
browser_open + browser_read, one click that must come back as
needs_confirmation (the "Buy now" danger upgrade), and one plain click.
Finally it opens a second page tab and drives the read-only tab survey
(listTabs / activeTab): both page tabs listed, the link tab filtered, the
TabInfo shape (id, windowId, title<=200, url, active, pinned, audible,
incognito, lastAccessed) intact, and activeTab = the newest active tab.

Everything on the scratch port is OUR process: stub first, bridge after;
nothing on the machine besides our PIDs is touched.

Exits 0 on success, 1 on any failure, 3 if firefox/web-ext are missing.
"""
import functools
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
PAGE2 = """<!doctype html><html><head><title>Riko headless test 2</title></head>
<body><h1>Second tab for the tab survey</h1></body></html>"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DownStub(http.server.BaseHTTPRequestHandler):
    """Stands on the bridge port while the desktop app is 'down': /hello
    404s (the probe must treat that as offline) and any other GET — a /link
    tab opening — is recorded so the check can fail loudly."""

    hits = []

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path.startswith("/hello"):
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format, *args):  # quiet
        pass


def main():
    firefox = shutil.which("firefox") or shutil.which("firefox-esr") or shutil.which("firefox-esp")
    npx = shutil.which("npx")
    if not firefox or not npx:
        print("SKIP: firefox and/or npx not found")
        return 3

    # Own scratch port: the fresh profile is pointed at it through the
    # options page's auto-save query, so a live desktop on 8766 or another
    # Firefox's helper can never join (or fake) this check.
    ws_port = free_port()
    page_port = free_port()
    page_dir = Path(tempfile.mkdtemp(prefix="riko-testpage-"))
    (page_dir / "index.html").write_text(PAGE)
    (page_dir / "second.html").write_text(PAGE2)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(page_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", page_port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{page_port}/index.html"
    page2_url = f"http://127.0.0.1:{page_port}/second.html"

    profile = Path(tempfile.mkdtemp(prefix="riko-headless-profile-"))
    # Seeding storage.local: Firefox 151 no longer reads webextensions.*
    # prefs (managed storage moved to native manifests / policies), so we
    # pin the legacy JSONFile storage backend and pre-create the extension's
    # storage file ourselves. --keep-profile-changes below is REQUIRED:
    # without it web-ext copies the profile and rewrites our files.
    (profile / "user.js").write_text(
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
        'user_pref("dom.webnotifications.enabled", false);\n'
        'user_pref("extensions.webextensions.ExtensionStorageIDB.enabled", false);\n')
    ext_store = profile / "browser-extension-data" / "riko-helper@riko.local"
    ext_store.mkdir(parents=True)
    (ext_store / "storage.js").write_text(json.dumps(
        {"token": TOKEN, "port": ws_port}))

    # desktop DOWN: our stub owns the scratch port and refuses /hello.
    # The token/port ride the seeded storage file (see above), so this
    # check never shares 8766 with a live desktop or the owner's helper.
    DownStub.hits = []
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", ws_port), DownStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    log_path = Path(os.environ.get("TMPDIR", "/tmp")) / "riko-web-ext.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [npx, "--yes", "web-ext", "run", "--verbose", "--target", "firefox-desktop",
         "--firefox", firefox, "--firefox-profile", str(profile),
         "--keep-profile-changes", "--no-reload", "--args=-headless"],
        cwd=REPO / "browser-ext", stdout=log, stderr=subprocess.STDOUT)

    bridge = None
    ok = False
    try:
        # Phase 1: desktop down for ~6 s. Allowed traffic: /hello probes.
        # Forbidden: anything else (a GET /link means a tab was opened).
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if proc.poll() is not None:
                print("FAIL: web-ext run died during phase 1 (see", log_path, ")")
                return 1
            wrong = [p for p in DownStub.hits if not p.startswith("/hello")]
            assert not wrong, f"extension opened a tab while the desktop was down: {wrong}"
            time.sleep(0.5)
        wrong = [p for p in DownStub.hits if not p.startswith("/hello")]
        assert not wrong, f"extension opened a tab while the desktop was down: {wrong}"
        print(f"desktop DOWN for 6 s: no link tab opened "
              f"({len(DownStub.hits)} probe attempts seen, all /hello)")

        # Phase 2: the desktop comes up on the same port. The supervisor's
        # probe (next alarm within ~1 min) must open the tab by itself.
        stub.shutdown()
        stub.server_close()   # release the port so the real bridge can bind it
        bridge = BrowserBridge(TOKEN, "127.0.0.1", ws_port)
        logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="[ws] %(message)s")
        bridge.start()
        is_helper = lambda: bridge.connected() and bridge.extension_name() == "Riko Helper"  # noqa: E731
        deadline = time.time() + 120
        while time.time() < deadline and not is_helper():
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if not is_helper():
            print("FAIL: the extension never connected after the desktop came up (see", log_path, ")")
            return 1
        link_hits = [p for p in DownStub.hits if p.startswith("/link")]
        print(f"extension connected: {bridge.extension_name()} over ws://127.0.0.1:{ws_port} "
              f"(link tab: {link_hits or 'served before stub switch'})")

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
        assert not any("Riko link" in (t[1] or "") for t in titles), \
            "the link tab leaked into browser_tabs numbering"

        # Phase 3: read-only tab survey (desktop awareness, DESKTOP_SENSE.md).
        # Open a SECOND tab so listTabs must show two page tabs, then check
        # the documented shape: link tab filtered, no incognito in a plain
        # session, titles capped, and activeTab = the newest (active) tab.
        opened2 = bridge.call("open", {"url": page2_url, "new_tab": True})
        print("browser_open(second) ->", opened2)
        assert opened2.get("ok") is True, "opening the second tab failed"
        time.sleep(1.0)  # let the new tab settle

        lt = bridge.call("listTabs", {})
        print("listTabs ->", json.dumps(lt.get("result"), ensure_ascii=False)[:600])
        assert lt.get("ok") is True, f"listTabs failed: {lt}"
        entries = lt["result"]
        assert isinstance(entries, list) and entries, "listTabs returned nothing"
        need = {"id", "windowId", "title", "url", "active", "pinned",
                "audible", "incognito", "lastAccessed"}
        missing = [e for e in entries if not need.issubset(e)]
        assert not missing, f"TabInfo fields missing: {missing[:1]}"
        urls = [e["url"] or "" for e in entries]
        assert any(u.startswith(page_url) for u in urls), f"first page tab missing: {urls}"
        assert page2_url in urls, f"second page tab missing: {urls}"
        link_pref = f"http://127.0.0.1:{ws_port}/link"
        assert not any(u.startswith(link_pref) for u in urls), \
            f"the link tab leaked into listTabs: {urls}"
        assert not any(f"127.0.0.1%3A{ws_port}%2Flink" in u for u in urls), \
            "the link tab's error page leaked into listTabs"
        assert all(e["incognito"] is False for e in entries), \
            "incognito flag wrong in a plain session"
        assert all(len(e["title"]) <= 200 for e in entries), "title over the 200-char cap"
        assert sum(1 for e in entries if e["active"]) >= 1

        at = bridge.call("activeTab", {})
        print("activeTab ->", json.dumps(at.get("result"), ensure_ascii=False))
        assert at.get("ok") is True, f"activeTab failed: {at}"
        act = at["result"]
        assert isinstance(act, dict), f"activeTab returned {act!r}, expected the active tab"
        assert set(act) == need, f"activeTab TabInfo shape wrong: {sorted(act)}"
        assert act["url"] == page2_url, \
            f"activeTab is {act['url']!r}, expected the just-opened {page2_url!r}"
        assert act["active"] is True and act["incognito"] is False
        assert act["id"] in [e["id"] for e in entries], "activeTab id not in listTabs"
        ok = True
    finally:
        proc.send_signal(subprocess.SIGINT if hasattr(subprocess, "SIGINT") else 2)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["pkill", "-f", str(profile)], capture_output=True)
        if bridge is not None:
            bridge.stop()
        httpd.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(page_dir, ignore_errors=True)

    print("HEADLESS CHECK PASSED" if ok else "HEADLESS CHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
