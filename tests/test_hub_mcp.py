"""MCP client tests (docs/ROUNDS.md R2-M).

The in-repo stdio fixture (tests/fixtures/mcp_echo_server.py, two tools) is
driven through the real hub: settings PUT -> live connection -> POST
/api/chat with a scripted fake LLM that calls mcp_echo__echo. Permissions
(never / ask through the __approval__ client channel), error and timeout
handling, crash -> error -> restart, write-only env values and the status
route are all exercised end to end. Unit tests cover naming, risk mapping
and the "mcp package not installed" fallback.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub import mcpmanager
from hub.server import create_app
from test_hub_tools import ScriptedLLM, call_step

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py")
READY_DEADLINE_S = 30.0

_APPS = []   # apps built this test; their MCP loop threads get shut down


@pytest.fixture(autouse=True)
def _mcp_app_teardown():
    yield
    while _APPS:
        try:
            _APPS.pop().state.mcp.shutdown()
        except Exception:
            pass


def echo_server(name="echo", **extra) -> dict:
    srv = {"name": name, "transport": "stdio", "enabled": True,
           "command": sys.executable, "args": [FIXTURE]}
    srv.update(extra)
    return srv


def mcp_client(tmp_path, steps, servers=None, **app_kw):
    """A hub wired to a scripted LLM and (optionally) MCP servers, saved
    through PUT /api/settings like the web page does."""
    llm = ScriptedLLM(steps)
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     **app_kw)
    _APPS.append(app)
    client = TestClient(app)
    if servers is not None:
        r = client.put("/api/settings", json={"mcp": {"servers": servers}})
        assert r.status_code == 200, r.text
    client._mcp_llm = llm   # tests read llm.chat_requests off it
    return client


def wait_state(client, name, want, timeout=READY_DEADLINE_S):
    """Poll /api/mcp/status until `name` reports `want` (or the deadline)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        rows = {r["name"]: r for r in client.get("/api/mcp/status").json()}
        last = rows.get(name)
        if last and last["state"] == want:
            return last
        time.sleep(0.2)
    raise AssertionError(f"server '{name}' never reached '{want}': {last}")


def mcp_ready(tmp_path, servers=None, steps=None):
    client = mcp_client(tmp_path, steps or [{"content": "ok"}],
                        servers if servers is not None else [echo_server()])
    wait_state(client, "echo", "ready")
    return client


# ── units: naming, risk, payload ────────────────────────────────────────────

def test_model_tool_name_shape():
    assert mcpmanager.model_tool_name("echo", "add_note") == "mcp_echo__add_note"
    weird = mcpmanager.model_tool_name("my.server!", "get time/2")
    assert weird == "mcp_my_server___get_time_2"
    import re
    long_tool = mcpmanager.model_tool_name("s", "x" * 300)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", long_tool)


def test_risk_from_annotations():
    A = mcpmanager.load_sdk()  # sdk present in this venv
    from mcp.types import ToolAnnotations
    assert A is not None
    assert mcpmanager.risk_for_annotations(ToolAnnotations(read_only_hint=True)) == "read"
    assert mcpmanager.risk_for_annotations(ToolAnnotations(destructive_hint=True)) == "danger"
    assert mcpmanager.risk_for_annotations(
        ToolAnnotations(read_only_hint=True, destructive_hint=True)) == "read"
    assert mcpmanager.risk_for_annotations(ToolAnnotations(title="x")) == "act"
    assert mcpmanager.risk_for_annotations(None) == "act"


def test_validate_servers_rules():
    good = [{"name": "t", "transport": "stdio", "command": "uvx", "args": ["x"]}]
    assert mcpmanager.validate_servers(good, [])[0]["command"] == "uvx"
    for bad in ([{"name": "Bad Name", "transport": "stdio", "command": "x"}],
                [{"name": "a", "transport": "stdio", "command": "x"},
                 {"name": "a", "transport": "http", "url": "http://x"}],
                [{"name": "a", "transport": "stdio"}],           # no command
                [{"name": "a", "transport": "websocket", "url": "http://x"}],
                [{"name": "a", "transport": "http", "url": "ftp://x"}],
                [{"name": "a", "transport": "stdio", "command": "x", "args": "y"}],
                [{"name": "a", "transport": "stdio", "command": "x",
                  "tool_permissions": {"t": "sometimes"}}],
                [{"name": "a", "transport": "stdio", "command": "x", "timeout": 0}]):
        with pytest.raises(ValueError):
            mcpmanager.validate_servers(bad, [])
    out = mcpmanager.validate_servers(
        [{"name": "a", "transport": "stdio", "command": "x",
          "tool_permissions": {"t": None}}], [])
    assert out[0]["tool_permissions"] == {}


# ── settings semantics: write-only env/header values, reset ────────────────

def test_mcp_settings_roundtrip_and_write_only(tmp_path):
    client = mcp_client(tmp_path, [{"content": "hi"}], servers=None)
    st = client.get("/api/settings").json()
    assert st["mcp"] == {"servers": []}
    assert st["mcp_source"] == {"servers": "default"}

    srv = {"name": "remote", "transport": "http", "url": "http://127.0.0.1:9/mcp",
           "env": {"TOKEN": "abc123"}, "headers": {"X-Key": "HeadTok-9f2c1"},
           "tool_permissions": {"x": "never"}, "timeout": 30}
    out = client.put("/api/settings", json={"mcp": {"servers": [srv]}}).json()
    got = out["mcp"]["servers"][0]
    assert "abc123" not in json.dumps(out) and "HeadTok-9f2c1" not in json.dumps(out)
    assert got["env"] == {"TOKEN": True} and got["headers"] == {"X-Key": True}
    assert got["tool_permissions"] == {"x": "never"} and got["timeout"] == 30
    assert out["mcp_source"] == {"servers": "saved"}
    raw = client.app.state.store.get_json("mcp", {})
    assert raw["servers"][0]["env"]["TOKEN"] == "abc123"

    # blank value for an existing key keeps the stored secret…
    srv2 = {**srv, "env": {"TOKEN": ""}, "headers": {"X-Key": ""}}
    client.put("/api/settings", json={"mcp": {"servers": [srv2]}})
    raw = client.app.state.store.get_json("mcp", {})
    assert raw["servers"][0]["env"]["TOKEN"] == "abc123"
    assert raw["servers"][0]["headers"]["X-Key"] == "HeadTok-9f2c1"
    # …a typed value replaces it, dropping the key removes it
    client.put("/api/settings", json={"mcp": {"servers": [
        {**srv, "env": {"TOKEN": "***", "OTHER": "2"}}]}})
    raw = client.app.state.store.get_json("mcp", {})
    assert raw["servers"][0]["env"] == {"TOKEN": "***", "OTHER": "2"}
    client.put("/api/settings", json={"mcp": {"servers": [{**srv, "env": {}}]}})
    assert client.app.state.store.get_json("mcp", {})["servers"][0]["env"] == {}

    # route validation surfaces as 422
    assert client.put("/api/settings", json={"mcp": {"servers": [{"name": "??"}]}}
                      ).status_code == 422
    # reset clears the whole block
    out = client.post("/api/settings/reset", json={"mcp": True}).json()
    assert out["mcp"] == {"servers": []}
    assert out["mcp_source"] == {"servers": "default"}


def test_status_and_restart_routes_without_servers(tmp_path):
    client = mcp_client(tmp_path, [{"content": "ok"}], servers=[])
    assert client.get("/api/mcp/status").json() == []
    assert client.post("/api/mcp/whatever/restart").status_code == 404
    client.put("/api/settings", json={"mcp": {"servers": [
        {**echo_server("offd"), "enabled": False}]}})
    rows = client.get("/api/mcp/status").json()
    assert rows == [{"name": "offd", "transport": "stdio", "state": "disabled",
                     "error": "", "tools": 0, "last_ok": ""}]
    assert client.post("/api/mcp/offd/restart").status_code == 409


def test_unavailable_when_sdk_missing(tmp_path, monkeypatch):
    """Local mode without the mcp package: the hub still works, MCP reports
    'unavailable: mcp package not installed' and chat is unaffected."""
    monkeypatch.setattr(mcpmanager, "sdk_available", lambda: False)
    monkeypatch.setattr(mcpmanager, "load_sdk", lambda: None)
    client = mcp_client(tmp_path, [{"content": "fine"}], servers=[echo_server()])
    rows = client.get("/api/mcp/status").json()
    assert rows[0]["state"] == "unavailable"
    assert rows[0]["error"] == "mcp package not installed"
    assert client.post("/api/chat", json={"text": "hi"}).json()["reply"] == "fine"


# ── the real connection: chat roundtrip through the stdio fixture ──────────

def test_chat_calls_mcp_tool_end_to_end(tmp_path):
    client = mcp_ready(tmp_path, steps=[
        call_step("mcp_echo__echo", {"text": "hello mcp"}),
        {"content": "it echoed back"},
    ])
    res = client.post("/api/chat", json={"text": "say hi to mcp"}).json()
    assert res["reply"] == "it echoed back"
    assert res["tools_used"] == [{"tool": "mcp_echo__echo",
                                  "label": "using echo: echo",
                                  "args": {"text": "hello mcp"}}]
    # the model saw the result, wrapped as untrusted
    tool_msgs = [m for m in client._mcp_llm.chat_requests[1]["messages"]
                 if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and "echo: hello mcp" in tool_msgs[0]["content"]
    assert "untrusted" in tool_msgs[0]["content"]
    offered = [t["function"]["name"] for t in client._mcp_llm.chat_requests[0]["tools"]]
    assert "mcp_echo__echo" in offered and "mcp_echo__add_note" in offered
    # tool log + events
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["tool"] == "mcp_echo__echo" and log[0]["source"] == "mcp"
    assert log[0]["ok"] is True and log[0]["approval"] == "not needed"
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    started = next(e for e in evs if e["type"] == "tool_started")
    assert started["tool"] == "mcp_echo__echo"
    assert started["label"] == "using echo: echo"


def test_api_tools_lists_mcp_tools_with_risk(tmp_path):
    client = mcp_ready(tmp_path)
    by = {t["name"]: t for t in client.get("/api/tools").json()["tools"]}
    echo = by["mcp_echo__echo"]
    assert echo["source"] == "mcp" and echo["risk"] == "read"
    assert echo["permission"] == "always" and echo["server"] == "echo"
    note = by["mcp_echo__add_note"]
    assert note["risk"] == "danger"            # destructiveHint
    assert note["permission"] == "ask"         # act/danger default
    # status row says ready with two tools and a timestamp
    row = {r["name"]: r for r in client.get("/api/mcp/status").json()}["echo"]
    assert row == {"name": "echo", "transport": "stdio", "state": "ready",
                   "error": "", "tools": 2, "last_ok": row["last_ok"]}
    assert row["last_ok"]


# ── permissions ─────────────────────────────────────────────────────────────

def test_never_permission_hides_the_tool(tmp_path):
    # per-server tool_permissions…
    client = mcp_ready(tmp_path, servers=[
        echo_server(tool_permissions={"add_note": "never"})])
    client.post("/api/chat", json={"text": "hi"})
    offered = [t["function"]["name"] for t in client._mcp_llm.chat_requests[0]["tools"]]
    assert "mcp_echo__echo" in offered and "mcp_echo__add_note" not in offered
    # …beats the global map saying always
    client.put("/api/settings", json={"tools": {"permissions":
                                                {"mcp_echo__add_note": "always"}}})
    client._mcp_llm.chat_requests.clear()
    client.post("/api/chat", json={"text": "hi again"})
    offered = [t["function"]["name"] for t in client._mcp_llm.chat_requests[0]["tools"]]
    assert "mcp_echo__add_note" not in offered
    # and the global map alone hides too
    client.put("/api/settings", json={"mcp": {"servers": [echo_server()]}})
    client.put("/api/settings", json={"tools": {"permissions":
                                                {"mcp_echo__echo": "never"}}})
    wait_state(client, "echo", "ready")
    client._mcp_llm.chat_requests.clear()
    client.post("/api/chat", json={"text": "hi third"})
    offered = [t["function"]["name"] for t in client._mcp_llm.chat_requests[0]["tools"]]
    assert "mcp_echo__echo" not in offered


def test_ask_routes_approval_through_client_channel(tmp_path):
    client = mcp_ready(tmp_path, steps=[
        call_step("mcp_echo__echo", {"text": "approved please"}),
        {"content": "done"},
    ], servers=[echo_server(tool_permissions={"echo": "ask"})])
    client.post("/api/clients/desktop1/register", json={
        "name": "desk", "kind": "desktop",
        "tools": [{"name": "open_url", "description": "open", "risk": "act",
                   "parameters": {"type": "object", "properties": {}}}]})
    seen: dict = {}

    def approve():
        r = client.get("/api/clients/desktop1/requests?wait=10")
        reqs = r.json()["requests"]
        seen["requests"] = reqs
        for req in reqs:
            client.post("/api/clients/desktop1/results",
                        json={"request_id": req["request_id"], "ok": True,
                              "approved": True})

    t = threading.Thread(target=approve, daemon=True)
    t.start()
    res = client.post("/api/chat", json={"text": "echo it"}).json()
    t.join(15)
    assert res["reply"] == "done"
    assert seen.get("requests"), "no approval request reached the client"
    approval_req = seen["requests"][0]
    assert approval_req["tool"] == "__approval__"
    assert "using echo: echo" in approval_req["arguments"]["summary"]
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["approval"] == "approved" and log[0]["ok"] is True


def test_ask_denied_surfaces_to_model(tmp_path):
    client = mcp_ready(tmp_path, steps=[
        call_step("mcp_echo__echo", {"text": "x"}), {"content": "denied"}],
        servers=[echo_server(tool_permissions={"echo": "ask"})])
    client.post("/api/clients/desktop1/register",
                json={"name": "desk", "kind": "desktop", "tools": []})

    def deny():
        reqs = client.get("/api/clients/desktop1/requests?wait=10").json()["requests"]
        for req in reqs:
            client.post("/api/clients/desktop1/results",
                        json={"request_id": req["request_id"], "ok": True,
                              "approved": False})

    t = threading.Thread(target=deny, daemon=True)
    t.start()
    res = client.post("/api/chat", json={"text": "echo it"}).json()
    t.join(15)
    assert res["reply"] == "denied"
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["ok"] is False and log[0]["approval"] == "not approved"
    tool_msg = next(m for m in client._mcp_llm.chat_requests[1]["messages"]
                    if m["role"] == "tool")
    assert "did not approve" in tool_msg["content"]


# ── errors, timeouts, crash + restart ───────────────────────────────────────

def test_tool_error_reaches_the_model(tmp_path):
    client = mcp_ready(tmp_path, steps=[
        call_step("mcp_echo__echo", {"text": "boom"}), {"content": "it failed"}],
        servers=[{**echo_server(), "env": {"ECHO_FAIL": "1"}}])
    res = client.post("/api/chat", json={"text": "echo"}).json()
    assert res["reply"] == "it failed"
    tool_msg = next(m for m in client._mcp_llm.chat_requests[1]["messages"]
                    if m["role"] == "tool")
    # the fixture answers unknown exceptions with isError + a generic text;
    # it must reach the model wrapped as an error, not as a success
    assert "Error executing tool echo" in tool_msg["content"]
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["ok"] is False and "Error executing tool" in log[0]["error"]


def test_per_call_timeout(tmp_path):
    client = mcp_ready(tmp_path, steps=[
        call_step("mcp_echo__echo", {"text": "wait"}), {"content": "gave up"}],
        servers=[{**echo_server(timeout=2), "env": {"ECHO_HANG": "30"}}])
    res = client.post("/api/chat", json={"text": "echo slowly"}).json()
    assert res["reply"] == "gave up"
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["ok"] is False and "timed out" in log[0]["error"]


def test_bad_command_goes_to_error_then_recovers(tmp_path):
    client = mcp_client(tmp_path, [{"content": "ok"}], servers=[
        {"name": "broken", "transport": "stdio", "enabled": True,
         "command": "/nonexistent/mcp-binary-42", "args": []}])
    row = wait_state(client, "broken", "error", timeout=20)
    assert row["error"]
    assert client.post("/api/mcp/broken/restart").status_code == 200
    # still the same broken binary: back to error, never crashes the hub
    row = wait_state(client, "broken", "error", timeout=20)
    assert client.get("/api/health").json()["ok"] is True


def test_crash_to_error_then_restart_recovers(tmp_path):
    client = mcp_ready(tmp_path, steps=[
        call_step("mcp_echo__echo", {"text": "die"}), {"content": "crashed?"}],
        servers=[{**echo_server(), "env": {"ECHO_CRASH": "1"}}])
    res = client.post("/api/chat", json={"text": "kill the server"}).json()
    assert res["reply"] == "crashed?"              # the call itself answered
    wait_state(client, "echo", "error", timeout=30)  # watchdog notices
    r = client.post("/api/mcp/echo/restart")
    assert r.status_code == 200
    row = wait_state(client, "echo", "ready", timeout=30)  # respawn works
    assert row["state"] == "ready"


def test_sync_restarts_changed_and_drops_removed(tmp_path):
    client = mcp_ready(tmp_path)
    # disabling tears the session down; re-enabling brings it back
    client.put("/api/settings", json={"mcp": {"servers": [
        {**echo_server(), "enabled": False}]}})
    wait_state(client, "echo", "disabled")
    assert client.get("/api/tools").json()["tools"] and all(
        t["source"] != "mcp" for t in client.get("/api/tools").json()["tools"])
    client.put("/api/settings", json={"mcp": {"servers": [echo_server()]}})
    wait_state(client, "echo", "ready")
    # removing the server drops it from status entirely
    client.put("/api/settings", json={"mcp": {"servers": []}})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and client.get("/api/mcp/status").json():
        time.sleep(0.2)
    assert client.get("/api/mcp/status").json() == []


def test_http_transport_end_to_end(tmp_path):
    """The same fixture served over streamable-HTTP: the hub connects to a
    URL just as well as to a subprocess."""
    import socket
    import subprocess
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, FIXTURE, "--http", str(port)],
                            stderr=subprocess.DEVNULL)
    try:
        client = mcp_client(tmp_path, [
            call_step("mcp_echo__echo", {"text": "over http"}),
            {"content": "http works"},
        ], servers=[{"name": "echohttp", "transport": "http", "enabled": True,
                     "url": f"http://127.0.0.1:{port}/mcp"}])
        wait_state(client, "echohttp", "ready", timeout=45)
        res = client.post("/api/chat", json={"text": "echo over http"}).json()
        assert res["reply"] == "http works"
        assert res["tools_used"][0]["tool"] == "mcp_echo__echo"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_hub_boots_with_garbage_mcp_json(tmp_path):
    """A hand-edited database must not brick startup: a non-list `servers`
    value reads as no servers, and the manager simply has nothing to run."""
    client = mcp_client(tmp_path, [{"content": "ok"}], servers=None)
    client.app.state.store.set_json("mcp", {"servers": {"not": "a list"}})
    assert client.get("/api/settings").json()["mcp"] == {"servers": []}
    assert client.get("/api/mcp/status").json() == []
