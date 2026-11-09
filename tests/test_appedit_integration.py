"""App-editing pack integration (docs/TYPING.md): registered through the
same client_tools.register_extra hook as typing so it rides the awareness
gate, and its diff approvals driven end-to-end through
ClientRuntime._dispatch_request with a fake owner approval — approve
writes exactly once (confirmed=True re-call), deny writes nothing, and a
hub-side approval never asks the owner twice. Fake Obsidian REST server on
an ephemeral port + fake home only: never 127.0.0.1:27123/27124, never a
real kate, never keystrokes (the conftest guards hold)."""
import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from hub.tools.registry import tools_prompt
from riko import appedit, client_tools as ct
from riko.appedit import obsidian as ob

KEY = "integration-test-key"
NOTE = "hello world\nsecond line\n"
FILE_TXT = "alpha beta\nomega\n"


class TinyObsidian:
    """Minimal Local REST API stand-in: GET/PUT /vault/<path>, records
    every call so write counts are assertable."""

    def __init__(self, vault):
        self.vault = dict(vault)
        self.log = []  # (method, path)
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _run(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = unquote(self.path.split("?", 1)[0])
                outer.log.append((method, path))
                payload, status = b"", 404
                if path.startswith("/vault/"):
                    name = path[len("/vault/"):]
                    if method == "GET" and name in outer.vault:
                        payload, status = outer.vault[name].encode(), 200
                    elif method == "PUT":
                        outer.vault[name] = body.decode("utf-8", "replace")
                        status = 204
                self.send_response(status)
                if status == 200:
                    self.send_header("Content-Type", "text/markdown")
                    self.send_header("ETag", "etag-1")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._run("GET")

            def do_PUT(self):
                self._run("PUT")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def puts(self):
        return [p for m, p in self.log if m == "PUT"]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def obsidian_server():
    srv = TinyObsidian({"notes.md": NOTE})
    ob.set_client(ob.ObsidianRest(key=KEY, base_url=srv.base_url))
    yield srv
    ob.set_client(None)
    srv.stop()


@pytest.fixture
def kate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes.txt").write_text(FILE_TXT)
    return tmp_path


class FakeHubClient:
    def __init__(self, sink):
        self.sink = sink

    def client_result(self, cid, rid, **kw):
        self.sink.append((rid, kw))


def make_runtime(executors, approve):
    """Enough of a ClientRuntime for _dispatch_request (test_typing.py
    pattern): records every approval question and every hub result."""
    rt = object.__new__(ct.ClientRuntime)
    rt.executors = dict(executors)
    rt.client_id = "test-client"
    rt.approval_timeout = 10.0
    rt.results = []
    rt.asked = []
    rt.hub = FakeHubClient(rt.results)
    rt.ask_approval = lambda summary, timeout=None: (rt.asked.append(summary)
                                                     or approve)
    return rt


def dispatch(rt, tool, args, needs_approval=False):
    ct.ClientRuntime._dispatch_request(
        rt, {"request_id": f"r-{tool}", "tool": tool, "arguments": args,
             "needs_approval": needs_approval,
             "summary": f"{tool} {args}"}, 10.0)
    return rt.results[-1][1]


# ── obsidian_edit end-to-end through _dispatch_request ─────────
def test_obsidian_edit_approve_writes_exactly_once(obsidian_server):
    rt = make_runtime(appedit.executors(sense=None), approve=True)
    out = dispatch(rt, "obsidian_edit",
                   {"path": "notes.md", "find": "second line",
                    "replace": "SECOND"})
    assert out["ok"] is True and out["approved"] is True
    assert "SECOND" in obsidian_server.vault["notes.md"]
    # exactly one PUT: the NeedConfirmation re-call with confirmed=True
    assert obsidian_server.puts() == ["/vault/notes.md"]
    # one question to the owner, showing a real diff
    assert len(rt.asked) == 1
    assert "-second line" in rt.asked[0] and "+SECOND" in rt.asked[0]


def test_obsidian_edit_deny_writes_nothing(obsidian_server):
    rt = make_runtime(appedit.executors(sense=None), approve=False)
    out = dispatch(rt, "obsidian_edit",
                   {"path": "notes.md", "find": "second line",
                    "replace": "SECOND"})
    assert out["ok"] is False and out["approved"] is False
    assert "denied" in out["error"]
    assert obsidian_server.vault["notes.md"] == NOTE
    assert obsidian_server.puts() == []
    assert len(rt.asked) == 1


def test_obsidian_edit_hub_approved_asks_once_no_double_prompt(obsidian_server):
    """needs_approval from the hub already asked the owner: the runtime
    injects confirmed=True, so the executor must NOT raise a second
    prompt — one ask, one write."""
    rt = make_runtime(appedit.executors(sense=None), approve=True)
    out = dispatch(rt, "obsidian_edit",
                   {"path": "notes.md", "find": "second line",
                    "replace": "SECOND"}, needs_approval=True)
    assert out["ok"] is True
    assert len(rt.asked) == 1
    assert obsidian_server.puts() == ["/vault/notes.md"]


# ── kate_edit end-to-end ───────────────────────────────────────
def test_kate_edit_approve_writes_once_with_backup(kate_home):
    rt = make_runtime(appedit.executors(sense=None, home=kate_home),
                      approve=True)
    out = dispatch(rt, "kate_edit",
                   {"path": "notes.txt", "find": "omega", "replace": "OMEGA"})
    assert out["ok"] is True
    assert (kate_home / "notes.txt").read_text() == "alpha beta\nOMEGA\n"
    assert len(rt.asked) == 1
    assert "-omega" in rt.asked[0] and "+OMEGA" in rt.asked[0]
    # one backup of the previous content, nothing else written
    backs = [p.name for p in kate_home.iterdir() if ".riko-bak" in p.name]
    assert len(backs) == 1


def test_kate_edit_deny_writes_nothing(kate_home):
    rt = make_runtime(appedit.executors(sense=None, home=kate_home),
                      approve=False)
    out = dispatch(rt, "kate_edit",
                   {"path": "notes.txt", "find": "omega", "replace": "OMEGA"})
    assert out["ok"] is False and out["approved"] is False
    assert (kate_home / "notes.txt").read_text() == FILE_TXT
    assert not [p for p in kate_home.iterdir() if ".riko-bak" in p.name]
    assert len(rt.asked) == 1


# ── registration rides the awareness gate ──────────────────────
def test_install_rides_awareness_gate():
    on = SimpleNamespace(awareness=SimpleNamespace(enabled=True),
                         executors=lambda: {})
    off = SimpleNamespace(awareness=SimpleNamespace(enabled=False),
                          executors=lambda: {})
    provider = appedit.install(on)
    try:
        names_on = {t["name"] for t in ct.desktop_tools(on)}
        assert {"obsidian_edit", "obsidian_search", "kate_edit",
                "kate_open", "kate_new", "find_file"} <= names_on
        ex = ct.make_executors(None, on)
        assert {"obsidian_edit", "kate_edit", "find_file"} <= set(ex)
        assert "obsidian_edit" not in {t["name"] for t in ct.desktop_tools(off)}
        assert "kate_edit" not in ct.make_executors(None, off)
    finally:
        ct.unregister_extra(provider)
    assert "obsidian_edit" not in {t["name"] for t in ct.desktop_tools(on)}


# ── hub prompt line ────────────────────────────────────────────
def test_prompt_line_only_with_appedit_tools():
    with_line = tools_prompt(["pc__desktop_windows", "pc__obsidian_edit",
                              "pc__kate_edit", "pc__find_file"])
    assert "Obsidian" in with_line and "diff" in with_line
    assert "find_file" in with_line
    plain = tools_prompt(["pc__desktop_windows", "pc__type_text"])
    assert "Obsidian" not in plain


# ── the extended conftest guard itself ─────────────────────────
def test_guard_blocks_obsidian_ports_on_every_dial_path():
    for port in (27123, 27124):
        with pytest.raises(RuntimeError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)
        with pytest.raises(RuntimeError):
            http.client.HTTPConnection("127.0.0.1", port)
        with pytest.raises(RuntimeError):
            http.client.HTTPSConnection("localhost", port)
        s = socket.socket()
        try:
            with pytest.raises(RuntimeError):
                s.connect(("localhost", port))
        finally:
            s.close()


def test_guard_leaves_ephemeral_fake_servers_alone():
    import urllib.request
    srv = TinyObsidian({"x.md": "hi"})
    try:
        with urllib.request.urlopen(srv.base_url + "/vault/x.md",
                                    timeout=2) as r:
            assert r.read() == b"hi"
    finally:
        srv.stop()
