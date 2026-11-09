"""Obsidian Local REST API tools against a fake in-process server
(docs/TYPING.md: never the owner's real Obsidian on 27124/27123, stdlib
HTTP on a scratch port only)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import pytest

from riko import appedit
from riko.appedit import obsidian as ob
from riko.client_tools import NeedConfirmation, ToolError

GOOD_KEY = "sekrit-test-key"


class FakeObsidian:
    """In-memory vault + every route the client uses. Records requests so
    PATCH headers can be asserted."""

    def __init__(self, key=GOOD_KEY):
        self.key = key
        self.vault = {"active.md": "The active note.\nsecond line\n"}
        self.active = "active.md"
        self.log = []          # (method, raw_path, headers, body)
        self.last_patch = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _split(self, key):
        """'x/heading/Log' → (file='x', heading=['Log']) with
        percent-decoding; the file part ends at a '/heading/' marker."""
        low = key.lower()
        i = low.find("/heading/")
        if i >= 0:
            return unquote(key[:i]), [unquote(s) for s in key[i + 9:].split("/") if s]
        return unquote(key), None

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(self, code, body="", headers=None, ctype="text/markdown"):
                if isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                if code != 204:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD" and body:
                    self.wfile.write(body)

            def _err(self, code, msg):
                self._send(code, json.dumps({"message": msg}).encode(),
                           ctype="application/json")

            def _authed(self):
                """True / False (anonymous GET /) / None = 401 already sent."""
                auth = self.headers.get("Authorization") or ""
                if auth == f"Bearer {outer.key}":
                    return True
                if auth == "" and self.path.split("?")[0] == "/":
                    return False
                self._err(401, "Invalid API key")
                return None

            def _route(self, body):
                parts = urlsplit(self.path)
                raw = parts.path.lstrip("/")
                outer.log.append((self.command, self.path,
                                  dict(self.headers), body))
                if raw == "obsidian-local-rest-api.crt":
                    return self._send(200, b"-----FAKE CA-----\n")
                authed = self._authed()
                if authed is None:
                    return
                if raw == "":                    # GET / status
                    return self._send(200, json.dumps({
                        "status": "OK", "authenticated": authed,
                        "versions": {"obsidian": "1.6.0", "self": "3.0.0"}}).encode(),
                        ctype="application/json")
                if raw.startswith("open/"):
                    f = unquote(raw[5:])
                    if f not in outer.vault:
                        return self._err(404, f"no file {f}")
                    return self._send(204)
                if raw.startswith("search/simple/"):
                    q = (parts.query and
                         dict(p.split("=", 1) for p in parts.query.split("&")
                              if "=" in p).get("query")) or ""
                    ql = q.lower()
                    rows = [{"filename": f, "score": 1.0,
                             "matches": [{"start": 0, "end": 3}],
                             "match": t[:100], "totalMatches": t.lower().count(ql)}
                            for f, t in outer.vault.items() if ql in t.lower()]
                    return self._send(200, json.dumps(rows),
                                      ctype="application/json")
                loc = None
                heading = None
                f = ""
                if raw in ("active/", "active"):
                    f = outer.active
                    loc = f"/vault/{f}"
                elif raw.startswith("vault/"):
                    f, heading = outer._split(raw[len("vault/"):])
                else:
                    return self._err(404, f"no route {raw}")
                text = outer.vault.get(f)
                m = self.command
                if m == "GET":
                    if text is None:
                        return self._err(404, f"no file {f}")
                    hdrs = {"ETag": '"v7"',
                            **({"Content-Location": loc} if loc else {})}
                    return self._send(200, text, headers=hdrs)
                if m == "PUT":
                    outer.vault[f] = body.decode() if heading is None \
                        else outer.vault.get(f, "")
                    return self._send(204, headers={"ETag": '"v8"'})
                if m == "POST":
                    outer.vault[f] = outer.vault.get(f, "") + body.decode()
                    return self._send(204, headers={
                        **({"Content-Location": loc} if loc else {})})
                if m == "PATCH":
                    outer.last_patch = {"headers": dict(self.headers),
                                        "url": self.path,
                                        "body": body.decode()}
                    op = self.headers.get("Operation")
                    if op not in ("append", "prepend", "replace"):
                        return self._err(400, f"bad Operation {op!r}")
                    if heading is None:
                        return self._err(400, "no heading target in URL")
                    if text is None:
                        return self._err(404, f"no file {f}")
                    outer.vault[f] = (text + "\n" + body.decode()
                                      if op == "append" else
                                      body.decode() + "\n" + text
                                      if op == "prepend" else body.decode())
                    outer.last_patch["heading"] = heading
                    return self._send(204)
                if m == "DELETE":
                    if text is None:
                        return self._err(404, f"no file {f}")
                    outer.vault.pop(f)
                    return self._send(204)
                return self._err(405, "nope")

            def do_GET(self):
                self._route(b"")

            def _do_w(self):
                self._route(self._body())

            do_PUT = do_POST = do_PATCH = do_DELETE = _do_w

        return H


@pytest.fixture()
def server():
    s = FakeObsidian()
    yield s
    s.stop()


@pytest.fixture()
def client(server, monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_OBSIDIAN_CONFIG", str(tmp_path / "obsidian.json"))
    ob.set_client(ob.ObsidianRest(key=GOOD_KEY, base_url=server.base_url))
    yield server
    ob.set_client(None)


# ── status ─────────────────────────────────────────────────────
def test_status_reports_reachable_authed_and_active_file(client):
    out = ob.obsidian_status()
    assert out == {"reachable": True, "authenticated": True,
                   "versions": {"obsidian": "1.6.0", "self": "3.0.0"},
                   "active_file": "active.md"}


def test_status_bad_key_is_clear_and_down_is_clear(client):
    ob.set_client(ob.ObsidianRest(key="wrong", base_url=client.base_url))
    out = ob.obsidian_status()
    assert out["reachable"] is False
    assert "API key" in out["error"] and GOOD_KEY not in out["error"]
    client.stop()
    out = ob.obsidian_status()
    assert out["reachable"] is False
    assert "Local REST API" in out["error"]


# ── open / create ──────────────────────────────────────────────
def test_open_hits_post_open_with_newleaf(client):
    client.vault["Ideas/a b.md"] = "x"
    out = ob.obsidian_open("Ideas/a b.md", new_leaf=True)
    assert "opened" in out and "new leaf" in out
    assert client.log[-1][0] == "POST"
    assert "/open/Ideas/a%20b.md" in client.log[-1][1]
    with pytest.raises(ToolError, match="no such file"):
        ob.obsidian_open("missing.md")


def test_create_refuses_overwrite_then_allows(client):
    out = ob.obsidian_create("New/Note.md", "hello")
    assert "created" in out and client.vault["New/Note.md"] == "hello"
    with pytest.raises(ToolError, match="already exists"):
        ob.obsidian_create("New/Note.md", "boom")
    out = ob.obsidian_create("New/Note.md", "boom", overwrite=True)
    assert "replaced" in out and client.vault["New/Note.md"] == "boom"


# ── read / append ──────────────────────────────────────────────
def test_read_path_and_active_with_cap(client):
    client.vault["active.md"] = "a" * 30_000
    out = ob.obsidian_read()
    assert out["path"] == "active.md" and out["truncated"] is True
    assert out["chars"] == 30_000 and len(out["text"]) < 30_000
    client.vault["small.md"] = "short text"
    out = ob.obsidian_read(path="small.md")
    assert out["text"] == "short text" and not out["truncated"]


def test_append_path_and_active(client):
    assert ob.obsidian_append(" more", path="active.md")
    assert client.vault["active.md"].endswith(" more")
    out = ob.obsidian_append(" tail")          # active file via /active/
    assert "active.md" in out
    assert client.vault["active.md"].endswith(" tail")
    assert any(m == "POST" and p.startswith("/active/") for m, p, *_ in client.log)


# ── edit (exact once, diff approval) ───────────────────────────
def test_edit_requires_confirmation_with_diff_then_applies(client):
    with pytest.raises(NeedConfirmation) as ei:
        ob.obsidian_edit(path="active.md", find="second line", replace="SECOND")
    reason = ei.value.reason
    assert "active.md" in reason and "-second line" in reason and "+SECOND" in reason
    out = ob.obsidian_edit(path="active.md", find="second line",
                           replace="SECOND", confirmed=True)
    assert "edited active.md" in out
    assert "SECOND" in client.vault["active.md"]
    assert client.log[-1][0] == "PUT"


def test_edit_zero_and_multiple_match_errors(client):
    client.vault["p.md"] = "dup dup solo"
    with pytest.raises(ToolError, match="0 matches"):
        ob.obsidian_edit(path="p.md", find="nope", replace="x")
    with pytest.raises(ToolError, match="2 matches"):
        ob.obsidian_edit(path="p.md", find="dup", replace="x")
    assert client.vault["p.md"] == "dup dup solo"


def test_edit_active_file_uses_put_active(client):
    with pytest.raises(NeedConfirmation):
        ob.obsidian_edit(find="second line", replace="SECOND")
    ob.obsidian_edit(find="second line", replace="SECOND", confirmed=True)
    assert "SECOND" in client.vault["active.md"]
    assert any(m == "PUT" and p.startswith("/active/") for m, p, *_ in client.log)


# ── heading insert via PATCH ───────────────────────────────────
def test_insert_under_heading_uses_operation_header(client):
    client.vault["daily.md"] = "# Log\n- one\n"
    out = ob.obsidian_insert("daily.md", "Log", "- two")
    assert "append" in out
    patch = client.last_patch
    assert patch["headers"]["Operation"] == "append"
    assert patch["url"] == "/vault/daily.md/heading/Log"
    assert patch["heading"] == ["Log"]
    assert client.vault["daily.md"].endswith("- two")


def test_insert_nested_heading_and_operation_validation(client):
    client.vault["w.md"] = "# Work\n## Meetings\nbody\n"
    ob.obsidian_insert("w.md", "Work > Meetings", "- synced",
                       operation="prepend")
    assert client.last_patch["url"] == "/vault/w.md/heading/Work/Meetings"
    with pytest.raises(ToolError, match="append | prepend | replace"):
        ob.obsidian_insert("w.md", "Work", "x", operation="shuffle")


# ── search ─────────────────────────────────────────────────────
def test_search_shapes_results(client):
    client.vault["todo.md"] = "buy milk and eggs"
    out = ob.obsidian_search("milk")
    files = [r["file"] for r in out["results"]]
    assert "todo.md" in files
    assert all("snippet" in r for r in out["results"])


# ── errors, key handling, transport guard ──────────────────────
def test_bad_key_on_write_is_clear(client):
    ob.set_client(ob.ObsidianRest(key="nope", base_url=client.base_url))
    with pytest.raises(ToolError) as ei:
        ob.obsidian_read(path="active.md")
    assert "401" in str(ei.value) and "obsidian-key" in str(ei.value)
    assert GOOD_KEY not in str(ei.value)


def test_missing_path_is_404_message(client):
    with pytest.raises(ToolError, match="no such file"):
        ob.obsidian_read(path="gone.md")


def test_connection_refused_message_mentions_plugin(server, monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_OBSIDIAN_CONFIG", str(tmp_path / "obsidian.json"))
    port = server.port
    server.stop()
    ob.set_client(ob.ObsidianRest(key=GOOD_KEY,
                                  base_url=f"http://127.0.0.1:{port}"))
    with pytest.raises(ToolError) as ei:
        ob.obsidian_read(path="x.md")
    assert "Local REST API" in str(ei.value)
    ob.set_client(None)


def test_base_url_must_be_loopback():
    with pytest.raises(ToolError, match="loopback"):
        ob.ObsidianRest(key="k", base_url="https://evil.example.com")


def test_key_file_is_0600_and_never_logged(monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_OBSIDIAN_CONFIG", str(tmp_path / "cfg" / "obsidian.json"))
    path = ob.save_key("abc123")
    assert path.stat().st_mode & 0o777 == 0o600
    assert ob.key_is_set()
    # save_key keeps other settings entries
    ob.save_key("abc124", base_url="http://127.0.0.1:27123")
    assert ob.load_settings()["base_url"] == "http://127.0.0.1:27123"


def test_no_key_saves_a_point_to_the_dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_OBSIDIAN_CONFIG", str(tmp_path / "obsidian.json"))
    ob.set_client(ob.ObsidianRest(key="", base_url="http://127.0.0.1:1"))
    with pytest.raises(ToolError, match="obsidian-key"):
        ob.obsidian_read(path="x.md")
    ob.set_client(None)


def test_catalogue_shape_and_registration_surface():
    names = [t["name"] for t in appedit.tools()]
    assert {"obsidian_status", "obsidian_open", "obsidian_create",
            "obsidian_read", "obsidian_append", "obsidian_edit",
            "obsidian_insert", "obsidian_search", "kate_open", "kate_new",
            "kate_tabs", "kate_edit", "find_file"} <= set(names)
    for t in appedit.tools():
        assert t["risk"] in ("read", "act", "danger")
        assert t["parameters"]["type"] == "object"
        assert t["description"]
    ex = appedit.executors()
    assert set(names) <= set(ex) and all(callable(v) for v in ex.values())
