"""The desktop side of the client-tool channel (R2-F): ClientRuntime against
FakeHub, and the browser relay against a fake Riko Helper WebSocket client.

Covers: register/heartbeat, request -> execute -> result, the http(s)-only
guard on open_url, approvals (yes / no / timeout / no-UI), `__approval__`
(hub-tool approvals routed to us), the /api/events progress relay, danger
self-upgrades from the extension, and the extension token auth.
"""
import json
import threading
import time

import pytest

from conftest import free_port, make_cfg
from riko import client_tools, hub_client

CID = "test-desktop"
TOOLS = [{"name": "open_url", "risk": "read", "description": "open",
          "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}},
         {"name": "clipboard_read", "risk": "act", "description": "paste",
          "parameters": {"type": "object", "properties": {}}}]


class FakeExtension:
    """A stand-in for the Riko Helper Firefox extension (one WebSocket)."""

    def __init__(self, bridge, token="tok", answers=None):
        self.answers = answers or {}
        self.seen = []           # relayed commands, in order
        self.token = token
        self.bridge = bridge
        self.replies = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()

    def _run(self):
        from websockets.sync.client import connect
        try:
            with connect(f"ws://127.0.0.1:{self.bridge.port}") as ws:
                ws.send(json.dumps({"type": "hello", "token": self.token,
                                    "name": "Fake Helper"}))
                first = json.loads(ws.recv())
                self.replies.append(first)
                if first.get("type") != "hello_ok":
                    self.ready.set()
                    return
                self.ready.set()
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=5)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if "id" not in msg:
                        continue
                    self.seen.append(msg)
                    answer = self.answers.get(msg.get("method"),
                                              {"ok": True, "result": "done"})
                    if callable(answer):
                        answer = answer(msg)
                    ws.send(json.dumps({"id": msg["id"], **answer}))
        except Exception as e:  # wrong token close etc.
            self.replies.append({"error": str(e)})
            self.ready.set()

    def start(self):
        self._thread.start()
        assert self.ready.wait(5), "fake extension never completed the handshake"

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=7)


@pytest.fixture
def runtime(fake_hub, hub_connected):
    """A factory: ClientRuntime wired to FakeHub, registered, torn down after."""
    made = []

    def make(client_id=CID, tools=None, executors=None, **kw):
        kw.setdefault("poll_wait", 0.1)
        kw.setdefault("heartbeat_interval", 0.05)
        rt = client_tools.ClientRuntime(hub_connected, tools or TOOLS,
                                        executors or {}, client_id=client_id, **kw)
        made.append(rt)
        rt.start()
        assert fake_hub.wait_for_register(client_id), "runtime never registered"
        return rt

    yield make
    for rt in made:
        rt.stop()


@pytest.fixture
def fake_opener(monkeypatch):
    """pretend xdg-open exists and succeeds; record every argv."""
    calls = []
    monkeypatch.setattr(client_tools.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("xdg-open", "notify-send") else None)

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(client_tools.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or Done())
    return calls


# ── identity files ───────────────────────────────────────────
def test_client_id_and_token_are_stable_and_private(tmp_path, monkeypatch):
    import stat
    monkeypatch.setenv("RIKO_CONFIG_DIR", str(tmp_path))
    cid = client_tools.client_id()
    assert client_tools.client_id() == cid
    tok = client_tools.browser_token()
    assert len(tok) >= 16 and client_tools.browser_token() == tok
    mode = stat.S_IMODE((tmp_path / "browser_token").stat().st_mode)
    assert mode == 0o600


# ── registration / heartbeat ─────────────────────────────────
def test_register_announces_tools(runtime, fake_hub):
    runtime()
    body = fake_hub.registered[CID]
    assert body["kind"] == "desktop"
    assert [t["name"] for t in body["tools"]] == ["open_url", "clipboard_read"]


def test_heartbeat_keeps_us_online(runtime, fake_hub):
    runtime()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not fake_hub.heartbeats:
        time.sleep(0.05)
    assert fake_hub.heartbeats and fake_hub.heartbeats[-1] == CID


# ── execution ────────────────────────────────────────────────
def test_open_url_executes_and_reports(runtime, fake_hub, fake_opener):
    rt = runtime(executors=client_tools.make_executors(None))
    req = fake_hub.push_request(CID, "open_url", {"url": "https://docs.godotengine.org"})
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and res["ok"] and res["approved"] is True
    assert "opened" in res["result"]
    assert fake_opener and fake_opener[0][0] == "/usr/bin/xdg-open"
    assert fake_opener[0][-1] == "https://docs.godotengine.org"


def test_open_url_refuses_non_http(runtime, fake_hub, fake_opener):
    rt = runtime(executors=client_tools.make_executors(None))
    req = fake_hub.push_request(CID, "open_url", {"url": "file:///etc/passwd"})
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and not res["ok"] and "http" in res["error"]
    assert fake_opener == []  # never even tried to launch a browser


def test_unknown_tool_reports_error(runtime, fake_hub):
    runtime(executors={})
    req = fake_hub.push_request(CID, "open_url", {"url": "https://x"})
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and not res["ok"] and "open_url" in res["error"]


# ── approvals ────────────────────────────────────────────────
def test_approval_yes_runs_the_tool(runtime, fake_hub):
    asked = []

    def on_approval(summary, respond, timeout):
        asked.append(summary)
        respond(True)

    rt = runtime(on_approval=on_approval,
                 executors={"clipboard_read": lambda: "clipboard contents"})
    req = fake_hub.push_request(CID, "clipboard_read", {},
                                needs_approval=True, summary="read the clipboard")
    res = fake_hub.wait_for_result(req["request_id"])
    assert asked == ["read the clipboard"]
    assert res and res["ok"] and res["approved"] is True
    assert res["result"] == "clipboard contents"


def test_approval_no_denies(runtime, fake_hub):
    rt = runtime(on_approval=lambda s, respond, t: respond(False),
                 executors={"clipboard_read": lambda: pytest.fail("must not run")})
    req = fake_hub.push_request(CID, "clipboard_read", {}, needs_approval=True)
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and not res["ok"] and res["approved"] is False


def test_approval_timeout_is_a_deny(runtime, fake_hub):
    rt = runtime(on_approval=lambda s, respond, t: None,  # nobody answers
                 approval_timeout=0.3,
                 executors={"clipboard_read": lambda: "x"})
    req = fake_hub.push_request(CID, "clipboard_read", {}, needs_approval=True)
    res = fake_hub.wait_for_result(req["request_id"], timeout=5)
    assert res and not res["ok"] and res["approved"] is False


def test_no_ui_means_deny(runtime, fake_hub):
    rt = runtime(executors={"clipboard_read": lambda: "x"})
    req = fake_hub.push_request(CID, "clipboard_read", {}, needs_approval=True)
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and not res["ok"] and res["approved"] is False


def test_ctl_can_answer_the_open_prompt(runtime, fake_hub):
    # riko-ctl approve lands through resolve_approval
    answered = threading.Event()

    def on_approval(summary, respond, timeout):
        answered.set()
        # the UI waits; we answer from 'outside' like riko-ctl would

    rt = runtime(on_approval=on_approval,
                 executors={"clipboard_read": lambda: "x"})
    req = fake_hub.push_request(CID, "clipboard_read", {}, needs_approval=True)
    assert answered.wait(3)
    assert rt.pending_approval() is not None
    assert rt.resolve_approval(True)
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and res["ok"] and res["approved"] is True


def test_hub_tool_approval_roundtrip(runtime, fake_hub):
    asked = []
    rt = runtime(on_approval=lambda s, respond, t: asked.append(s) or respond(True))
    req = fake_hub.push_request(
        CID, "__approval__", {"summary": "fetch_page with a risky url"},
        needs_approval=False)
    res = fake_hub.wait_for_result(req["request_id"])
    assert asked == ["fetch_page with a risky url"]
    assert res and res["ok"] and res["approved"] is True


# ── events -> UI progress ────────────────────────────────────
def test_events_are_forwarded_for_progress(runtime, fake_hub):
    seen = []
    rt = runtime(on_event=lambda kind, ev: seen.append((kind, ev)))
    fake_hub.push_event("tool_started", call_id="c1", tool="web_search",
                        label="searching the web")
    fake_hub.push_event("turn_finished", persona="riko", message_id=1,
                        emotion="happy")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(k == "turn_finished" for k, _ in seen):
        time.sleep(0.05)
    kinds = [k for k, _ in seen]
    assert "tool_started" in kinds and "turn_finished" in kinds


# ── browser relay ────────────────────────────────────────────
@pytest.fixture
def bridge():
    b = client_tools.BrowserBridge("tok", "127.0.0.1", free_port())
    b.start()
    yield b
    b.stop()


def test_extension_bad_token_is_rejected(bridge):
    ext = FakeExtension(bridge, token="wrong")
    ext.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ext.replies:
        time.sleep(0.05)
    assert any("reject" in json.dumps(r) or "bad token" in json.dumps(r)
               for r in ext.replies)
    assert not bridge.connected()


def test_browser_tool_relay_roundtrip(runtime, fake_hub, bridge):
    ext = FakeExtension(bridge, answers={"read": {"ok": True,
                                                  "result": {"title": "Hello", "elements": []}}})
    ext.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bridge.connected():
        time.sleep(0.05)
    assert bridge.connected()
    browser_tools = [{"name": "browser_read", "risk": "read", "description": "read",
                      "parameters": {"type": "object", "properties": {}}}]
    rt = runtime(tools=browser_tools, executors=client_tools.make_executors(bridge))
    req = fake_hub.push_request(CID, "browser_read", {})
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and res["ok"] and res["result"]["title"] == "Hello"
    assert ext.seen and ext.seen[-1]["method"] == "read"


def test_danger_upgrade_always_asks(runtime, fake_hub, bridge):
    asked = []

    def click_answer(msg):
        if msg.get("params", {}).get("confirmed"):
            return {"ok": True, "result": "clicked"}
        return {"ok": False, "needs_confirmation": True,
                "reason": "click 'Buy now' — this may cost money"}

    ext = FakeExtension(bridge, answers={"click": click_answer})
    ext.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bridge.connected():
        time.sleep(0.05)
    tools = [{"name": "browser_click", "risk": "act", "description": "click",
              "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}]
    rt = runtime(tools=tools, executors=client_tools.make_executors(bridge),
                 on_approval=lambda s, respond, t: asked.append(s) or respond(True))
    req = fake_hub.push_request(CID, "browser_click", {"n": 7})  # needs_approval: False!
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and res["ok"] and res["result"] == "clicked"
    assert asked and "Buy now" in asked[0]           # the danger question was shown
    assert ext.seen[0]["params"].get("confirmed") is not True
    assert ext.seen[1]["params"].get("confirmed") is True


def test_browser_tool_without_extension_reports_not_connected(runtime, fake_hub, bridge):
    tools = [{"name": "browser_tabs", "risk": "read", "description": "tabs",
              "parameters": {"type": "object", "properties": {}}}]
    rt = runtime(tools=tools, executors=client_tools.make_executors(bridge))
    req = fake_hub.push_request(CID, "browser_tabs", {})
    res = fake_hub.wait_for_result(req["request_id"])
    assert res and not res["ok"] and "not connected" in res["error"]


def test_start_desktop_glue(fake_hub, hub_connected, tmp_path, monkeypatch):
    """desktop.py's one call: start_desktop registers the full catalogue, the
    approval bubble event fires, and the approve/deny hooks answer it."""
    events = []

    class StubViewer:
        @staticmethod
        def emit(kind, **data):
            events.append((kind, data))

    monkeypatch.setenv("RIKO_CONFIG_DIR", str(tmp_path / "cfg"))
    cfg = {"desktop": {"client": {"browser_port": free_port(),
                                  "approval_timeout": 5}}}
    rt, br = client_tools.start_desktop(cfg, hub_connected, viewer_module=StubViewer)
    try:
        assert rt is not None and br is not None
        assert fake_hub.wait_for_register(rt.client_id)
        tools = [t["name"] for t in fake_hub.registered[rt.client_id]["tools"]]
        assert {"open_url", "clipboard_read", "clipboard_write", "notify",
                "browser_read", "browser_click"} <= set(tools)
        # the per-install token file is there and readable only by us
        token_file = tmp_path / "cfg" / "browser_token"
        assert token_file.exists() and oct(token_file.stat().st_mode & 0o777) == "0o600"
        req = fake_hub.push_request(rt.client_id, "__approval__",
                                   {"summary": "paste your clipboard"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and rt.pending_approval() is None:
            time.sleep(0.05)
        assert any(k == "approval" for k, _ in events), "no approval bubble event"
        assert rt.approve_hook()["answered"] is True
        res = fake_hub.wait_for_result(req["request_id"])
        assert res and res["ok"] and res["approved"] is True
        assert any(k == "approval_done" for k, _ in events)
    finally:
        rt.stop()
        br.stop()


def test_start_desktop_disabled(fake_hub, hub_connected, tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_CONFIG_DIR", str(tmp_path / "cfg"))
    rt, br = client_tools.start_desktop({"desktop": {"client": {"enabled": False}}},
                                        hub_connected, viewer_module=None)
    assert rt is None and br is None


def test_link_page_is_served(bridge):
    """GET /link hosts the extension's WebSocket tab (http origin, so Firefox
    doesn't force-upgrade its ws:// to wss://). GET /riko-pair hands out the
    token for auto-pairing."""
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{bridge.port}/link", timeout=5) as r:
        body = r.read().decode()
        assert r.headers["Content-Type"].startswith("text/html")
    assert "Riko Helper" in body and "riko-link" in body
    with urllib.request.urlopen(f"http://127.0.0.1:{bridge.port}/riko-pair", timeout=5) as r:
        assert r.read().decode() == "tok"


# ── body capabilities (docs/BODY.md §1/§5) ───────────────────────────
def test_register_announces_the_default_body(runtime, fake_hub):
    runtime()  # until the avatar page reports its real library, the
    body = fake_hub.registered[CID]["body"]  # built-in gesture list is registered
    assert body == {"actions": client_tools.DEFAULT_BODY_ACTIONS,
                    "expressions": client_tools.DEFAULT_BODY_EXPRESSIONS}
    assert "wave" in body["actions"] and "talk_1" not in body["actions"]


def test_body_report_reregisters_and_normalises(runtime, fake_hub, monkeypatch):
    monkeypatch.setattr(client_tools, "_current_body",
                        dict(client_tools._current_body))  # keep the module global clean
    rt = runtime()
    n0 = fake_hub.register_count
    out = rt.set_body({"actions": ["Curtsy", "shake head", "Curtsy", "", "bad!name"],
                       "expressions": ["HAPPY"]})
    assert out["ok"] and out["changed"]
    assert out["actions"] == ["curtsy", "shake_head"]  # normalized, deduped, junk dropped
    assert fake_hub.register_count == n0 + 1            # re-registered with the hub
    body = fake_hub.registered[CID]["body"]
    assert body["actions"] == ["curtsy", "shake_head"]
    assert body["expressions"] == ["happy"]
    assert client_tools.current_body_actions() == ["curtsy", "shake_head"]
    # the same library again: quiet, no re-register
    again = rt.set_body({"actions": ["curtsy", "Shake-Head"], "expressions": ["happy"]})
    assert again["changed"] is False
    assert fake_hub.register_count == n0 + 1


def test_runtime_can_start_with_a_reported_body(runtime, fake_hub, monkeypatch):
    monkeypatch.setattr(client_tools, "_current_body",
                        dict(client_tools._current_body))
    runtime(body={"actions": ["curtsy"], "expressions": ["happy"]})
    assert fake_hub.registered[CID]["body"]["actions"] == ["curtsy"]
    assert client_tools.current_body_actions() == ["curtsy"]  # greeting waves follow it
