"""Home Assistant via MCP tests (docs/API.md "MCP servers" / README
"Home Assistant").

Covers the built-in 'homeassistant' preset (RIKO_HA_URL + RIKO_HA_TOKEN env
defaults, permission default 'always'), ${NAME} secret placeholders in MCP
env/header values (expanded from the hub process environment at connect
time, never stored/logged/returned expanded), MCP Prompts injected into the
system prompt with caps, and readable Home Assistant progress labels.

The fake HA server (tests/fixtures/mcp_ha_server.py) mirrors the official
integration as probed live: /api/mcp/assist path, domain-prefixed tool names
(intent__HassTurnOn, homeassistant__GetLiveContext, ...), one no-argument
'Assist' Prompt long enough to cross the per-server cap, and a whoami tool
echoing the Authorization header it received.
"""
import asyncio
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub import mcpmanager
from hub.server import create_app
from test_hub_tools import ScriptedLLM, call_step

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "mcp_ha_server.py")
READY_DEADLINE_S = 60.0
HINT_MARKER = "Home Assistant: when you talk to the owner"

_APPS = []   # apps built this test; their MCP loop threads get shut down


@pytest.fixture(autouse=True)
def _mcp_app_teardown():
    yield
    while _APPS:
        try:
            _APPS.pop().state.mcp.shutdown()
        except Exception:
            pass


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ha_fixture():
    """Start the fake HA MCP server; returns (proc, base_url)."""
    port = free_port()
    proc = subprocess.Popen([sys.executable, FIXTURE, "--http", str(port)],
                            stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}"


def ha_client(tmp_path, steps, servers=None, mcp_defaults=None, **app_kw):
    llm = ScriptedLLM(steps)
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     mcp_defaults=mcp_defaults, **app_kw)
    _APPS.append(app)
    client = TestClient(app)
    if servers is not None:
        r = client.put("/api/settings", json={"mcp": {"servers": servers}})
        assert r.status_code == 200, r.text
    client._mcp_llm = llm
    return client


def wait_state(client, name, want, timeout=READY_DEADLINE_S):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        rows = {r["name"]: r for r in client.get("/api/mcp/status").json()}
        last = rows.get(name)
        if last and last["state"] == want:
            return last
        time.sleep(0.2)
    raise AssertionError(f"server '{name}' never reached '{want}': {last}")


# ── units: ${NAME} expansion ────────────────────────────────────────────────

def test_env_placeholder_expansion(monkeypatch):
    monkeypatch.setenv("RIKO_TEST_SECRET", "s3cr3t-value")
    assert mcpmanager.expand_env_placeholders(
        "Bearer ${RIKO_TEST_SECRET}") == "Bearer s3cr3t-value"
    assert mcpmanager.expand_env_placeholders(
        "${RIKO_TEST_SECRET}${RIKO_TEST_SECRET}") == "s3cr3t-value" * 2
    # names that are not env-var shaped stay literal
    for lit in ("${lower_case}", "${9BAD}", "no placeholders", "${}", "$plain"):
        assert mcpmanager.expand_env_placeholders(lit) == lit
    # a valid name that is not set raises with the NAME (never a value)
    with pytest.raises(mcpmanager.MissingEnvError) as e:
        mcpmanager.expand_env_placeholders("Bearer ${RIKO_DEFINITELY_NOT_SET_X9}")
    assert str(e.value) == "missing env RIKO_DEFINITELY_NOT_SET_X9"
    assert mcpmanager.expand_env_map(
        {"Authorization": "Bearer ${RIKO_TEST_SECRET}"}) \
        == {"Authorization": "Bearer s3cr3t-value"}


def test_is_env_template():
    yes = ["${RIKO_HA_TOKEN}", "Bearer ${RIKO_HA_TOKEN}", "${A}${B}",
           "x${AB1}y${C_2}", "50%${X}%"]
    no = ["", "literal", "Bearer abc.def", "${lower}", "${9X}", "p$ss",
          "$5 dollars", "${" + "R" + "O" * 64 + "}", "${A"]
    for v in yes:
        assert mcpmanager.is_env_template(v), v
    for v in no:
        assert not mcpmanager.is_env_template(v), v


def test_public_servers_placeholder_visible():
    servers = [{"name": "ha", "transport": "http", "url": "http://x",
                "env": {"LITERAL": "abc123", "TMPL": "${RIKO_HA_TOKEN}"},
                "headers": {"Authorization": "Bearer ${RIKO_HA_TOKEN}",
                            "X-Key": "HeadTok-9f2c1", "Empty": ""}}]
    pub = mcpmanager.public_servers(servers)[0]
    # raw placeholder text is not a secret -> shown; real values stay masked
    assert pub["env"] == {"LITERAL": True, "TMPL": "${RIKO_HA_TOKEN}"}
    assert pub["headers"] == {"Authorization": "Bearer ${RIKO_HA_TOKEN}",
                              "X-Key": True, "Empty": False}


def test_validate_default_permission():
    base = {"name": "a", "transport": "http", "url": "http://x"}
    assert mcpmanager.validate_servers(
        [{**base, "default_permission": "always"}], [])[0]["default_permission"] == "always"
    for dp in (None, ""):
        assert "default_permission" not in mcpmanager.validate_servers(
            [{**base, "default_permission": dp}], [])[0]
    assert "default_permission" not in mcpmanager.validate_servers([base], [])[0]
    with pytest.raises(ValueError):
        mcpmanager.validate_servers([{**base, "default_permission": "maybe"}], [])


def test_progress_labels():
    P = mcpmanager.progress_label
    # real HA names carry a domain prefix; the matcher strips it
    assert P("homeassistant", "intent__HassTurnOn", {"name": "Kitchen light"}) \
        == "turning on Kitchen light"
    assert P("homeassistant", "intent__HassTurnOff", {"area": "kitchen"}) \
        == "turning off the kitchen"
    assert P("someother", "climate__HassClimateSetTemperature",
             {"name": "Thermostat"}) == "setting the temperature of Thermostat"
    assert P("someother", "light__HassLightSet", {"name": "Desk"}) \
        == "setting the light Desk"
    assert P("someother", "intent__HassSetPosition", {"name": "Blind"}) \
        == "setting the position of Blind"
    assert P("someother", "homeassistant__GetLiveContext", {}) \
        == "checking the house"
    assert P("someother", "assist_satellite__HassBroadcast",
             {"area": "upstairs"}) == "announcing to upstairs"
    assert P("someother", "assist_satellite__HassBroadcast", {}) == "announcing"
    # unknown Hass tool or no usable args -> readable fallback
    assert P("homeassistant", "intent__HassMystery", {"name": "x"}) \
        == "using Home Assistant"
    assert P("homeassistant", "intent__HassTurnOn", {}) == "using Home Assistant"
    # non-HA tools and servers keep the classic label
    assert P("echo", "echo", {"text": "hi"}) == "using echo: echo"
    assert P("someother", "llm__GetDateTime", {}) == "using someother: llm__GetDateTime"


# ── units: prompt blocks ────────────────────────────────────────────────────

def _mkrt(mgr, name, prompt_text, tool="intent__HassTurnOn"):
    rt = mcpmanager._Runtime({"name": name, "transport": "http",
                              "url": "http://x", "enabled": True})
    rt.state = "ready"
    rt.prompt_text = prompt_text
    rt.tools = [{"name": tool, "model_name": f"mcp_{name}__{tool}",
                 "description": "", "input_schema": {}, "risk": "act"}]
    mgr._servers[name] = rt
    return rt


def test_prompt_notes_caps_and_hint():
    mgr = mcpmanager.MCPManager()
    _mkrt(mgr, "homeassistant", "A" * mcpmanager.MAX_PROMPT_CHARS_PER_SERVER)
    _mkrt(mgr, "other", "B" * mcpmanager.MAX_PROMPT_CHARS_PER_SERVER, tool="t")
    offered = ["mcp_homeassistant__intent__HassTurnOn", "mcp_other__t"]
    notes = mgr.prompt_notes(offered)
    assert notes.startswith("Instructions from homeassistant:\n")
    assert "Instructions from other:" in notes
    # the two blocks share the 3000-char budget (hint rides on top)
    assert len(notes) <= (mcpmanager.MAX_PROMPT_TOTAL_CHARS + 2
                          + len(mcpmanager.HA_SPEECH_HINT))
    # servers whose tools are not offered contribute nothing, no hint either
    assert mgr.prompt_notes(["mcp_homeassistant__intent__HassTurnOn"]) \
        .startswith("Instructions from homeassistant:")
    assert mgr.prompt_notes(["web_search"]) == ""
    # a ready server without prompts but HA tools offered -> hint alone
    mgr2 = mcpmanager.MCPManager()
    _mkrt(mgr2, "homeassistant", "")
    assert mgr2.prompt_notes(["mcp_homeassistant__intent__HassTurnOn"]) \
        == mcpmanager.HA_SPEECH_HINT


def test_refresh_prompts_skips_required_arguments():
    mgr = mcpmanager.MCPManager()
    rt = _mkrt(mgr, "ha", "")
    prompts = [
        types.SimpleNamespace(name="Assist", arguments=None, description=""),
        types.SimpleNamespace(name="needs", description="",
                              arguments=[types.SimpleNamespace(name="x",
                                                                required=True)]),
    ]
    got = types.SimpleNamespace(
        description="", messages=[types.SimpleNamespace(
            content=types.SimpleNamespace(type="text", text="hello world"))])

    class _C:
        async def list_prompts(self):
            return types.SimpleNamespace(prompts=prompts)

        async def get_prompt(self, name, arguments=None):
            assert name == "Assist"       # 'needs' must never be fetched
            return got
    rt.client = _C()
    asyncio.run(mgr._refresh_prompts(rt))
    assert rt.prompt_text == "[Assist]\nhello world"


# ── integration: secret placeholders over a real connection ────────────────

def test_secret_expands_on_connect_and_never_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_TEST_TOKEN", "sekrit-42")
    proc, base = ha_fixture()
    try:
        client = ha_client(tmp_path, [
            call_step("mcp_fakeha__whoami", {}),
            {"content": "saw it"},
        ], servers=[{"name": "fakeha", "transport": "http", "enabled": True,
                     "url": f"{base}/api/mcp/assist",
                     "headers": {"Authorization": "Bearer ${RIKO_TEST_TOKEN}"}}])
        wait_state(client, "fakeha", "ready")
        res = client.post("/api/chat", json={"text": "who am I"}).json()
        assert res["reply"] == "saw it"
        # the tool itself echoes the header it received: expansion is real
        tool_msg = next(m for m in client._mcp_llm.chat_requests[1]["messages"]
                        if m["role"] == "tool")
        assert "auth: Bearer sekrit-42" in tool_msg["content"]
        # the expanded value never comes back out of the hub
        for url in ("/api/settings", "/api/mcp/status", "/api/tools",
                    "/api/tools/log", "/api/events?after=0&wait=0",
                    "/api/personas"):
            assert "sekrit-42" not in client.get(url).text, url
        assert "sekrit-42" not in res["reply"]
        # GET keeps the RAW placeholder (a template is not a secret)
        srv = {s["name"]: s for s in
               client.get("/api/settings").json()["mcp"]["servers"]}["fakeha"]
        assert srv["headers"] == {"Authorization": "Bearer ${RIKO_TEST_TOKEN}"}
        # and it is what the database holds
        raw = client.app.state.store.get_json("mcp", {})
        assert raw["servers"][0]["headers"]["Authorization"] \
            == "Bearer ${RIKO_TEST_TOKEN}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_missing_env_var_reports_error_state_without_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("RIKO_DEFINITELY_NOT_SET_X9", raising=False)
    client = ha_client(tmp_path, [{"content": "ok"}], servers=[
        {"name": "noenv", "transport": "http", "enabled": True,
         "url": "http://127.0.0.1:9/mcp",
         "headers": {"Authorization": "Bearer ${RIKO_DEFINITELY_NOT_SET_X9}"}}])
    row = wait_state(client, "noenv", "error", timeout=20)
    assert row["error"] == "missing env RIKO_DEFINITELY_NOT_SET_X9"
    # restart pokes it; the variable is still missing -> same clean error
    assert client.post("/api/mcp/noenv/restart").status_code == 200
    row = wait_state(client, "noenv", "error", timeout=20)
    assert row["error"] == "missing env RIKO_DEFINITELY_NOT_SET_X9"
    assert client.get("/api/health").json()["ok"] is True
    # events carry the same message (names only, no class prefix, no value)
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    err_ev = [e for e in evs if e["type"] == "mcp_server_error"
              and e["server"] == "noenv"]
    assert err_ev and err_ev[-1]["error"] == "missing env RIKO_DEFINITELY_NOT_SET_X9"


# ── integration: the built-in homeassistant preset ─────────────────────────

HA_ENV = {"ha_url": "https://homeassistant.test", "ha_token": "tok-abc-123"}


def test_preset_appears_only_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_HA_TOKEN", "tok-abc-123")   # the real env var
    client = ha_client(tmp_path, [{"content": "ok"}], mcp_defaults=HA_ENV)
    st = client.get("/api/settings").json()
    srv = {s["name"]: s for s in st["mcp"]["servers"]}
    assert srv["homeassistant"] == {
        "name": "homeassistant", "transport": "http", "enabled": True,
        "url": "https://homeassistant.test/api/mcp/assist", "env": {},
        "headers": {"Authorization": "Bearer ${RIKO_HA_TOKEN}"},
        "default_permission": "always"}
    assert st["mcp_source"] == {"servers": "env"}
    assert st["defaults"]["mcp"]["servers"] == [srv["homeassistant"]]
    # the token VALUE never rides the API (only the placeholder does)
    assert "tok-abc-123" not in client.get("/api/settings").text
    # status lists it (config-only row; nothing reachable at ha.test)
    rows = {r["name"]: r for r in client.get("/api/mcp/status").json()}
    assert rows["homeassistant"]["transport"] == "http"
    # editing: PUT saves the block, the source flips to saved, and an
    # explicitly-listed homeassistant entry is never duplicated
    out = client.put("/api/settings", json={"mcp": {"servers": [
        {**srv["homeassistant"], "enabled": False}]}}).json()
    assert out["mcp_source"] == {"servers": "saved"}
    got = {s["name"]: s for s in out["mcp"]["servers"]}
    assert got["homeassistant"]["enabled"] is False
    assert client.get("/api/mcp/status").json()[0]["state"] == "disabled"
    # removing it entirely: the env default comes back on the next read
    out = client.put("/api/settings", json={"mcp": {"servers": []}}).json()
    assert [s["name"] for s in out["mcp"]["servers"]] == ["homeassistant"]
    # a saved list WITHOUT homeassistant still gets the env default appended
    out = client.put("/api/settings", json={"mcp": {"servers": [
        {"name": "time", "transport": "stdio", "command": sys.executable,
         "args": ["-c", "pass"]}]}}).json()
    assert sorted(s["name"] for s in out["mcp"]["servers"]) \
        == ["homeassistant", "time"]
    # reset drops the saved block -> back to the env default
    out = client.post("/api/settings/reset", json={"mcp": True}).json()
    assert out["mcp_source"] == {"servers": "env"}
    assert [s["name"] for s in out["mcp"]["servers"]] == ["homeassistant"]
    # no RIKO_HA_* env: no preset at all
    plain = ha_client(tmp_path, [{"content": "ok"}])
    st = plain.get("/api/settings").json()
    assert st["mcp"] == {"servers": []}
    assert st["defaults"]["mcp"]["servers"] == []
    assert st["mcp_source"] == {"servers": "default"}


def test_preset_connects_prompts_and_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_HA_TOKEN", "tok-abc-123")
    proc, base = ha_fixture()
    try:
        client = ha_client(tmp_path, [
            call_step("mcp_homeassistant__intent__HassTurnOn",
                      {"name": "Kitchen light"}),
            {"content": "the kitchen is bright"},
        ], servers=[], mcp_defaults={"ha_url": base, "ha_token": "tok-abc-123"})
        # servers=[] saves an empty list; the env default still rides along
        wait_state(client, "homeassistant", "ready")
        res = client.post("/api/chat",
                          json={"text": "turn on the kitchen light"}).json()
        assert res["reply"] == "the kitchen is bright"
        # readable progress label built from the tool args
        assert res["tools_used"] == [{"tool": "mcp_homeassistant__intent__HassTurnOn",
                                      "label": "turning on Kitchen light"}]
        evs = client.get("/api/events?after=0&wait=0").json()["events"]
        started = next(e for e in evs if e["type"] == "tool_started")
        assert started["label"] == "turning on Kitchen light"
        # permission default 'always' ran it with no approval (no client online)
        log = client.get("/api/tools/log").json()["entries"]
        assert log[0]["ok"] is True and log[0]["approval"] == "not needed"
        assert log[0]["tool"] == "mcp_homeassistant__intent__HassTurnOn"
        by = {t["name"]: t for t in client.get("/api/tools").json()["tools"]}
        hass = by["mcp_homeassistant__intent__HassTurnOn"]
        assert hass["permission"] == "always" and hass["risk"] == "act"
        live = by["mcp_homeassistant__homeassistant__GetLiveContext"]
        assert live["permission"] == "always"   # (risk comes from HA's annotations)
        # the Assist Prompt was fetched on connect and injected into the
        # system prompt, capped
        system = client._mcp_llm.chat_requests[0]["messages"][0]["content"]
        assert "Instructions from homeassistant:" in system
        assert "Smart Home MCP" in system               # the instruction text
        assert "END-OF-INSTRUCTIONS" not in system      # capped at 1500 chars
        assert HINT_MARKER in system                    # the HA speech hint
        # the raw settings still carry only the placeholder
        srv = {s["name"]: s for s in
               client.get("/api/settings").json()["mcp"]["servers"]}
        assert srv["homeassistant"]["headers"] \
            == {"Authorization": "Bearer ${RIKO_HA_TOKEN}"}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_ha_hint_follows_offered_tools(tmp_path):
    """Prompt text is injected whenever the server has one, but the HA
    speech hint only appears while HA-ish tools are actually on offer."""
    proc, base = ha_fixture()
    try:
        never = {n: "never" for n in (
            "intent__HassTurnOn", "intent__HassTurnOff", "light__HassLightSet",
            "climate__HassClimateSetTemperature", "intent__HassSetPosition",
            "intent__HassStopMoving", "intent__HassCancelAllTimers",
            "homeassistant__GetLiveContext", "assist_satellite__HassBroadcast")}
        client = ha_client(tmp_path, [{"content": "hi"}], servers=[
            {"name": "ha2", "transport": "http", "enabled": True,
             "url": f"{base}/api/mcp/assist"}])
        wait_state(client, "ha2", "ready")
        res = client.post("/api/chat", json={"text": "hi"}).json()
        assert res["reply"] == "hi"
        system = client._mcp_llm.chat_requests[0]["messages"][0]["content"]
        assert "Instructions from ha2:" in system
        assert HINT_MARKER in system        # Hass tools on offer -> hint
        # hide every HA tool; whoami/GetDateTime remain
        client.put("/api/settings", json={"mcp": {"servers": [
            {"name": "ha2", "transport": "http", "enabled": True,
             "url": f"{base}/api/mcp/assist", "tool_permissions": never}]}})
        wait_state(client, "ha2", "ready")
        res = client.post("/api/chat", json={"text": "hi again"}).json()
        assert res["reply"] == "hi"
        system = client._mcp_llm.chat_requests[-1]["messages"][0]["content"]
        assert "Instructions from ha2:" in system    # prompt still injected
        assert HINT_MARKER not in system             # but no HA tools -> no hint
    finally:
        proc.terminate()
        proc.wait(timeout=10)
