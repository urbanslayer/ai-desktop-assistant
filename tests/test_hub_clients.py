"""Client-tool channel tests (server side): register / heartbeat / long-poll
/ results / approvals / offline, through a REAL uvicorn socket so the
long-polls are genuine HTTP. A fake desktop thread plays the client.

Model-facing client tool names are `<kind>__<name>` (docs/ROUNDS.md R1-B).
"""
import threading
import time

import pytest
import requests
import uvicorn

from hub import clients as clients_mod
from hub.server import create_app
from test_hub_tools import ScriptedLLM, call_step


class LiveHub:
    """create_app() served by uvicorn on a free port (no auth key)."""

    def __init__(self, llm, tmp_path, tools_defaults=None):
        app = create_app(tmp_path / "hubdata",
                         llm_defaults={"base_url": llm.base_url, "model": "auto"},
                         tools_defaults=tools_defaults or {})
        cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("uvicorn did not start")
        self.base = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class FakeDesktop:
    """Polls /api/clients/{id}/requests in a thread and answers with `answer(request)`."""

    def __init__(self, hub, client_id="desk1", kind="desktop", tools=None,
                 answer=None):
        self.hub = hub
        self.id = client_id
        self.seen = []           # requests handed to answer()
        self.stop = threading.Event()
        self.answer = answer or (lambda req: {"ok": True, "approved": True,
                                              "result": "ran " + req["tool"]})
        r = requests.post(f"{hub.base}/api/clients/{client_id}/register",
                          json={"name": "my-desktop", "kind": kind,
                                "tools": tools if tools is not None else [
                                    {"name": "open_url", "risk": "read",
                                     "description": "Open a URL in the default browser",
                                     "parameters": {"type": "object", "properties":
                                                    {"url": {"type": "string"}}}},
                                    {"name": "do_thing", "risk": "act",
                                     "description": "Do a thing on the desktop"}]})
        r.raise_for_status()
        self.poll_errors = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                r = requests.get(f"{self.hub.base}/api/clients/{self.id}/requests",
                                 params={"wait": 2}, timeout=10)
                if r.status_code != 200:
                    self.poll_errors.append(r.status_code)
                    continue
                for req in r.json()["requests"]:
                    self.seen.append(req)
                    out = self.answer(req)
                    if out is None:      # pretend the human never answered
                        continue
                    requests.post(f"{self.hub.base}/api/clients/{self.id}/results",
                                  json={"request_id": req["request_id"], **out},
                                  timeout=10)
            except Exception as e:  # hub shutting down under us
                if not self.stop.is_set():
                    self.poll_errors.append(repr(e))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)


@pytest.fixture
def hub_desktop(tmp_path):
    """Fixture factory: build(steps, **kw) -> (client_requests_base, llm, desktop)."""
    made = []

    def build(steps, desktop=True, tools_defaults=None, answer=None):
        llm = ScriptedLLM(steps)
        made.append(llm)
        hub = LiveHub(llm, tmp_path, tools_defaults)
        made.append(hub)
        desk = None
        if desktop:
            desk = FakeDesktop(hub, answer=answer)
            made.append(desk)
        return hub, llm, desk

    yield build
    for m in reversed(made):
        if isinstance(m, FakeDesktop):
            m.close()
        else:
            m.stop()


def api(hub):
    return hub.base


# ── registration / visibility ───────────────────────────────────────────────

def test_register_validation_and_listing(tmp_path, hub_desktop):
    hub, llm, desk = hub_desktop([{"content": "hi"}])
    r = requests.get(f"{api(hub)}/api/clients")
    assert r.status_code == 200
    cl = r.json()["clients"]
    assert len(cl) == 1 and cl[0]["client_id"] == "desk1" and cl[0]["kind"] == "desktop"
    names = {t["model_name"]: t["risk"] for t in cl[0]["tools"]}
    assert names == {"desktop__open_url": "read", "desktop__do_thing": "act"}
    # /api/tools merges them with risk-based default permissions
    tools = {t["name"]: t for t in requests.get(f"{api(hub)}/api/tools").json()["tools"]}
    assert tools["desktop__open_url"]["permission"] == "always"
    assert tools["desktop__open_url"]["source"] == "client"
    assert tools["desktop__do_thing"]["permission"] == "ask"
    # validation (kind is normalised to lowercase, but must be a bare word)
    bad = {"name": "x", "kind": "desk top", "tools": []}
    assert requests.post(f"{api(hub)}/api/clients/d2/register", json=bad).status_code == 422
    bad = {"name": "x", "kind": "desktop", "tools": [{"name": "bad name!", "risk": "read"}]}
    assert requests.post(f"{api(hub)}/api/clients/d2/register", json=bad).status_code == 422
    bad = {"name": "x", "kind": "desktop", "tools": [{"name": "ok", "risk": "explode"}]}
    assert requests.post(f"{api(hub)}/api/clients/d2/register", json=bad).status_code == 422
    assert requests.post(f"{api(hub)}/api/clients/!*!/register",
                         json={"name": "x", "kind": "desktop", "tools": []}).status_code == 422
    # heartbeat + requests long-poll need registration first
    assert requests.post(f"{api(hub)}/api/clients/ghost/heartbeat").status_code == 404
    assert requests.get(f"{api(hub)}/api/clients/ghost/requests").status_code == 404
    assert requests.post(f"{api(hub)}/api/clients/desk1/heartbeat").json() == \
        {"ok": True, "heartbeat_s": clients_mod.HEARTBEAT_S}
    # unknown result request -> 404
    assert requests.post(f"{api(hub)}/api/clients/desk1/results",
                         json={"request_id": "nope", "ok": True}).status_code == 404


def test_offline_client_tools_disappear(tmp_path, hub_desktop):
    hub, llm, desk = hub_desktop([{"content": "hi"}])
    assert requests.delete(f"{api(hub)}/api/clients/desk1").status_code == 204
    assert requests.get(f"{api(hub)}/api/clients").json()["clients"] == []
    tools = requests.get(f"{api(hub)}/api/tools").json()["tools"]
    assert not any(t["name"].startswith("desktop__") for t in tools)
    # and the next chat does not offer it to the model
    requests.post(f"{api(hub)}/api/chat", json={"text": "hi"})
    offered = [t["function"]["name"] for t in llm.chat_requests[0].get("tools", [])]
    assert not any(n.startswith("desktop__") for n in offered)


def test_stale_client_counts_offline(tmp_path, hub_desktop, monkeypatch):
    hub, llm, desk = hub_desktop([{"content": "hi"}])
    monkeypatch.setattr(clients_mod, "OFFLINE_AFTER_S", -1.0)  # everyone is stale
    assert requests.get(f"{api(hub)}/api/clients").json()["clients"] == []
    tools = requests.get(f"{api(hub)}/api/tools").json()["tools"]
    assert not any(t["name"].startswith("desktop__") for t in tools)


# ── dispatch through the chat loop ──────────────────────────────────────────

def test_chat_runs_client_tool(tmp_path, hub_desktop):
    hub, llm, desk = hub_desktop([
        call_step("desktop__open_url", {"url": "https://docs.godotengine.org"}),
        {"content": "Opening the Godot docs for you!"},
    ])
    r = requests.post(f"{api(hub)}/api/chat", json={"text": "open the godot docs"},
                      timeout=30)
    res = r.json()
    assert res["reply"] == "Opening the Godot docs for you!"
    assert res["tools_used"] == [{"tool": "desktop__open_url",
                                  "label": 'open_url {"url": "https://docs.godotengine.org"}',
                                  "args": {"url": "https://docs.godotengine.org"}}]
    # the desktop really received and ran the request
    assert [q["tool"] for q in desk.seen] == ["open_url"]
    assert desk.seen[0]["arguments"] == {"url": "https://docs.godotengine.org"}
    assert desk.seen[0]["needs_approval"] is False
    # result went back to the model wrapped as untrusted data
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert "ran open_url" in tool_msgs[0]["content"]
    assert "untrusted" in tool_msgs[0]["content"]
    # logged as a client tool
    log = requests.get(f"{api(hub)}/api/tools/log").json()["entries"]
    assert log[0]["source"] == "client" and log[0]["ok"] is True
    assert log[0]["approval"] == "not needed"


def test_act_tool_gated_then_approved(tmp_path, hub_desktop):
    hub, llm, desk = hub_desktop([
        call_step("desktop__do_thing", {"x": 1}),
        {"content": "the thing is done"},
    ])
    r = requests.post(f"{api(hub)}/api/chat", json={"text": "do the thing"}, timeout=30)
    assert r.json()["reply"] == "the thing is done"
    assert desk.seen and desk.seen[0]["needs_approval"] is True
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert "ran do_thing" in tool_msgs[0]["content"]
    log = requests.get(f"{api(hub)}/api/tools/log").json()["entries"]
    assert log[0]["approval"] == "approved"
    # approval_needed showed up in the event stream
    evs = requests.get(f"{api(hub)}/api/events?after=0&wait=0").json()["events"]
    need = [e for e in evs if e["type"] == "approval_needed"]
    assert need and need[0]["tool"] == "desktop__do_thing"


def test_explicit_denial_answer(tmp_path, hub_desktop):
    def answer(req):
        return {"ok": True, "approved": False}
    hub, llm, desk = hub_desktop([
        call_step("desktop__do_thing", {}),
        {"content": "denied, got it"},
    ], answer=answer)
    res = requests.post(f"{api(hub)}/api/chat", json={"text": "do it"},
                        timeout=30).json()
    assert res["reply"] == "denied, got it"
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert "denied" in tool_msgs[0]["content"]
    log = requests.get(f"{api(hub)}/api/tools/log").json()["entries"]
    assert log[0]["approval"] == "denied"


def test_approval_nobody_answers_times_out(tmp_path, hub_desktop, monkeypatch):
    monkeypatch.setattr(clients_mod, "APPROVAL_TIMEOUT_S", 1)
    hub, llm, desk = hub_desktop([
        call_step("desktop__do_thing", {}),
        {"content": "no answer, so no action"},
    ], answer=lambda req: None)   # client sees the request but never answers
    res = requests.post(f"{api(hub)}/api/chat", json={"text": "do it"}, timeout=30).json()
    assert res["reply"] == "no answer, so no action"
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert "not approved within 1 s" in tool_msgs[0]["content"]
    log = requests.get(f"{api(hub)}/api/tools/log").json()["entries"]
    assert log[0]["approval"] == "not approved"


def test_client_tool_error_propagates(tmp_path, hub_desktop):
    hub, llm, desk = hub_desktop([
        call_step("desktop__open_url", {"url": "https://x"}),
        {"content": "that failed, sorry"},
    ], answer=lambda req: {"ok": False, "error": "no browser installed"})
    res = requests.post(f"{api(hub)}/api/chat", json={"text": "open x"}, timeout=30).json()
    assert res["reply"] == "that failed, sorry"
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert "no browser installed" in tool_msgs[0]["content"]
    log = requests.get(f"{api(hub)}/api/tools/log").json()["entries"]
    assert log[0]["ok"] is False and "no browser" in log[0]["error"]
