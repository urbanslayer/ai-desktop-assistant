"""Tool-loop, built-in tools, permissions, tool log and events tests.

A scripted fake LLM (JSON-mode function calling; streaming 400'd, like a
completion-only server) drives the loop; a fake SearXNG and a local page
server back web_search/fetch_page. SSRF is exercised through the real
guard: 127.0.0.1 is refused unless allow_private_fetch is on.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from hub import brain as brain_mod
from hub import toolloop
from hub.events import EventBus
from hub.server import TOOLS_DEFAULTS, create_app
from hub.tools import registry
from hub.tools import web as web_mod

L, R = chr(91), chr(93)  # [ ]


# ── fake servers ────────────────────────────────────────────────────────────

class ScriptedLLM:
    """Answers /v1/chat/completions from a step script. Each step:
    {"content": str, "tool_calls": [{id,name,arguments(str)}]}. The last step
    repeats forever. Streaming always 400s -> brain falls back to JSON."""

    def __init__(self, steps, reject_tools=False):
        self.steps = steps
        self.reject_tools = reject_tools
        self.chat_requests = []
        self.model_id = "scripted-model"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._send({"object": "list", "data": [
                        {"id": outer.model_id, "object": "model"}]})
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if body.get("stream"):
                    return self._send({"error": {"message": "streaming not supported"}}, 400)
                if outer.reject_tools and "tools" in body:
                    return self._send({"error": {"message": "this model does not support tools"}}, 400)
                i = len(outer.chat_requests)
                outer.chat_requests.append(body)
                step = outer.steps[min(i, len(outer.steps) - 1)]
                msg = {"role": "assistant", "content": step.get("content") or None}
                if step.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in step["tool_calls"]]
                finish = "tool_calls" if step.get("tool_calls") else "stop"
                return self._send({
                    "id": "x", "object": "chat.completion", "created": 0,
                    "model": outer.model_id,
                    "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSearxng:
    """GET /search?q&format=json -> canned results (SearXNG JSON shape)."""
    RESULTS = [{"title": "Tokyo Weather Now", "url": "https://wx.example/tokyo",
                "content": "21 C and sunny."},
               {"title": "Climate of Tokyo", "url": "https://wiki.example/tokyo",
                "content": "Humid subtropical."}]

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if self.path.startswith("/search"):
                    body = json.dumps({"results": FakeSearxng.RESULTS}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


PAGE_HTML = b"""<html><head><title>Stardust (1993 game)</title>
<style>body{color:red}</style><script>var x = "JUNKSCRIPT";</script></head>
<body><nav>navnoise</nav><h1>Stardust</h1><p>A 1993 video game by Topia.</p>
<p>Published by <a href="https://en.example/topia">Topia</a>.</p></body></html>"""


class FakePageServer:
    """A tiny site for fetch_page: /page, /go (302 -> /final), /bin (bytes)."""

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/page":
                    return self._send(200, "text/html; charset=utf-8", PAGE_HTML)
                if self.path == "/go":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                if self.path == "/final":
                    return self._send(200, "text/html", b"<html><body>FINAL TEXT</body></html>")
                if self.path == "/bin":
                    return self._send(200, "application/octet-stream", b"\x00\x01\x02\x03")
                return self._send(404, "text/plain", b"nope")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def searxng():
    s = FakeSearxng()
    yield s
    s.stop()


@pytest.fixture
def pages():
    s = FakePageServer()
    yield s
    s.stop()


def llm_client(tmp_path, steps, searxng=None, reject_tools=False, tools_defaults=None):
    llm = ScriptedLLM(steps, reject_tools=reject_tools)
    td = {"searxng_url": searxng.base_url if searxng else ""}
    td.update(tools_defaults or {})
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     tools_defaults=td)
    return TestClient(app), llm


def call_step(name, args, cid="c1"):
    return {"tool_calls": [{"id": cid, "name": name,
                            "arguments": json.dumps(args)}]}


# ── built-in tools direct ───────────────────────────────────────────────────

def test_web_search_parses_searxng(searxng):
    out = web_mod.web_search("tokyo weather", 5, searxng.base_url)
    assert out == [{"title": "Tokyo Weather Now", "url": "https://wx.example/tokyo",
                    "snippet": "21 C and sunny."},
                   {"title": "Climate of Tokyo", "url": "https://wiki.example/tokyo",
                    "snippet": "Humid subtropical."}]
    assert web_mod.web_search("x", 1, searxng.base_url) == [out[0]]
    with pytest.raises(RuntimeError, match="not configured"):
        web_mod.web_search("x", 5, "")


def test_fetch_page_reads_and_extracts(pages):
    out = web_mod.fetch_page(f"{pages.base}/page", allow_private=True)
    assert out["title"] == "Stardust (1993 game)"
    assert "A 1993 video game by Topia." in out["text"]
    assert "JUNKSCRIPT" not in out["text"] and "navnoise" not in out["text"]
    assert {"text": "Topia", "url": "https://en.example/topia"} in out["links"]
    # redirects are followed, guard runs on every hop
    out2 = web_mod.fetch_page(f"{pages.base}/go", allow_private=True)
    assert "FINAL TEXT" in out2["text"] and out2["url"].endswith("/final")
    bin_out = web_mod.fetch_page(f"{pages.base}/bin", allow_private=True)
    assert "not readable text" in bin_out["note"]


def test_fetch_page_caps_and_ssrf(pages, monkeypatch):
    with pytest.raises(Exception, match="private/loopback"):
        web_mod.fetch_page(f"{pages.base}/page")           # default: refuse 127.0.0.1
    with pytest.raises(Exception, match="only http/https"):
        web_mod.fetch_page("ftp://example.com/x", allow_private=True)
    monkeypatch.setattr(web_mod, "MAX_FETCH_BYTES", 40)
    out = web_mod.fetch_page(f"{pages.base}/page", allow_private=True)
    assert out["truncated"] is True


def test_get_time_zones():
    out = web_mod.get_time("Asia/Tokyo")
    assert out["timezone"] == "Asia/Tokyo" and "iso" in out and "weekday" in out
    out = web_mod.get_time("Not/AZone")
    assert out["timezone"] == "UTC" and "unknown timezone" in out["note"]


def test_registry_permission_policy():
    assert registry.permission_for("web_search", "hub", "read", {}) == "always"
    assert registry.permission_for("desktop__x", "client", "read", {}) == "always"
    assert registry.permission_for("desktop__x", "client", "act", {}) == "ask"
    assert registry.permission_for("desktop__x", "client", "danger", {}) == "ask"
    assert registry.permission_for("web_search", "hub", "read",
                                   {"web_search": "never"}) == "never"
    assert registry.full_tool_name("desktop", "open_url") == "desktop__open_url"
    with pytest.raises(ValueError):
        registry.full_tool_name("Desk Top", "open_url")


# ── settings: defaults, blank-unset, bounds, reset ─────────────────────────

def test_tools_settings_defaults_and_bounds(tmp_path, searxng):
    client, _ = llm_client(tmp_path, [{"content": "ok"}], tools_defaults={"searxng_url": "http://d"})
    st = client.get("/api/settings").json()
    assert st["tools"] == {**TOOLS_DEFAULTS, "searxng_url": "http://d"}
    assert st["tools_source"] == {k: "default" for k in TOOLS_DEFAULTS}
    # saved override wins
    r = client.put("/api/settings", json={"tools": {"max_steps": 3, "permissions": {"get_time": "ask"}}})
    assert r.json()["tools"]["max_steps"] == 3
    assert r.json()["tools_source"]["max_steps"] == "saved"
    assert r.json()["tools"]["permissions"] == {"get_time": "ask"}
    # blank searxng_url = unset the override -> back to the env/default
    client.put("/api/settings", json={"tools": {"searxng_url": "http://saved.example"}})
    r = client.put("/api/settings", json={"tools": {"searxng_url": ""}})
    assert r.json()["tools"]["searxng_url"] == "http://d"
    assert r.json()["tools_source"]["searxng_url"] == "default"
    # bounds and enum
    for bad in ({"max_steps": 0}, {"max_seconds": 4}, {"max_result_chars": 100},
                {"enabled": "yes"}, {"permissions": {"get_time": "sometimes"}}):
        assert client.put("/api/settings", json={"tools": bad}).status_code == 422
    # reset puts everything back
    r = client.post("/api/settings/reset", json={"tools": True})
    assert r.json()["tools"]["max_steps"] == TOOLS_DEFAULTS["max_steps"]
    assert r.json()["tools"]["permissions"] == {}


def test_api_tools_lists_builtins(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])  # no searxng url
    out = client.get("/api/tools").json()
    by = {t["name"]: t for t in out["tools"]}
    assert set(by) == {"web_search", "fetch_page", "get_time", "weather",
                       "remember", "recall", "forget_fact",
                       "timer", "list_timers", "cancel_timer"}  # memory round M, timers round H, weather round
    assert by["web_search"]["available"] is False   # no SearXNG configured
    assert by["get_time"]["available"] is True
    assert by["weather"]["risk"] == "read"          # keyless Open-Meteo, always offered
    assert all(t["permission"] == "always" and t["source"] == "hub" for t in by.values())
    assert out["status"]["function_calling"] in ("unknown", "ok")
    assert client.get("/api/tools/log").json() == {"entries": []}


# ── the loop through /api/chat ──────────────────────────────────────────────

def test_chat_tool_roundtrip_log_and_events(tmp_path, searxng):
    client, llm = llm_client(tmp_path, [
        call_step("web_search", {"query": "tokyo weather"}),
        {"content": f"{L}happy{R} It is 21 C and sunny in Tokyo today!"},
    ], searxng=searxng)
    res = client.post("/api/chat", json={"text": "weather in tokyo?"}).json()
    assert res["reply"] == "It is 21 C and sunny in Tokyo today!"
    assert res["emotion"] == "happy"
    assert res["tools_used"] == [{"tool": "web_search",
                                  "label": "searching the web for \u201ctokyo weather\u201d"}]
    # second model call saw an untrusted-wrapped tool result with the snippets
    second = llm.chat_requests[1]["messages"]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "c1"
    assert "untrusted" in tool_msgs[0]["content"] and "21 C and sunny" in tool_msgs[0]["content"]
    assert llm.chat_requests[0]["tools"][0]["function"]["name"] == "web_search"
    # system prompt advertises the tools + the current date
    assert "can call these tools" in llm.chat_requests[0]["messages"][0]["content"]
    # tool log got the full entry
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["tool"] == "web_search" and log[0]["ok"] is True
    assert log[0]["source"] == "hub" and "tokyo weather" in log[0]["arguments"]
    assert log[0]["approval"] == "not needed"
    # events: ordered, with the whole turn in the ring buffer
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    types = [e["type"] for e in evs]
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(seqs)
    for want in ("turn_started", "tool_started", "tool_finished", "turn_finished"):
        assert want in types, types
    assert types.index("tool_started") < types.index("tool_finished")
    started = next(e for e in evs if e["type"] == "tool_started")
    assert started["tool"] == "web_search" and started["call_id"] == "c1"
    done = next(e for e in evs if e["type"] == "turn_finished")
    assert done["emotion"] == "happy"
    # stored history: user + final reply + compact tools_used, no tool spam
    h = client.get("/api/history").json()["messages"]
    assert [m["role"] for m in h] == ["user", "assistant"]
    assert h[1]["tools_used"] == res["tools_used"]


def test_parallel_calls_two_tool_messages(tmp_path):
    client, llm = llm_client(tmp_path, [
        {"tool_calls": [{"id": "a", "name": "get_time", "arguments": "{}"},
                        {"id": "b", "name": "get_time",
                         "arguments": json.dumps({"timezone": "Asia/Tokyo"})}]},
        {"content": "time checked"},
    ])
    res = client.post("/api/chat", json={"text": "what time is it there and here?"}).json()
    assert res["reply"] == "time checked"
    assert [t["tool"] for t in res["tools_used"]] == ["get_time", "get_time"]
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert sorted(m["tool_call_id"] for m in tool_msgs) == ["a", "b"]


def test_unknown_tool_and_bad_json_reach_the_model(tmp_path):
    client, llm = llm_client(tmp_path, [
        call_step("no_such_tool", {}),
        {"tool_calls": [{"id": "c2", "name": "get_time",
                         "arguments": "this is not json{"}]},
        {"content": "recovered"},
    ])
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["reply"] == "recovered"
    contents = [m["content"] for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    assert "unknown tool" in contents[0]
    contents = [m["content"] for m in llm.chat_requests[2]["messages"] if m["role"] == "tool"]
    assert len(contents) == 2  # the earlier result is still in the history
    assert "malformed JSON arguments" in contents[1]
    log = client.get("/api/tools/log").json()["entries"]
    assert {e["tool"] for e in log} == {"no_such_tool", "get_time"}
    assert all(e["ok"] is False for e in log)


def test_max_steps_forces_final_plain_answer(tmp_path):
    # max_steps counts model calls INCLUDING the forced final answer:
    # with 3, one tool batch runs, the next request is dropped, then plain.
    client, llm = llm_client(tmp_path, [
        call_step("get_time", {}),
        call_step("get_time", {}),          # keeps insisting on tools
        {"content": "fine, here is my answer"},
    ])
    assert client.put("/api/settings", json={"tools": {"max_steps": 3}}).status_code == 200
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["reply"] == "fine, here is my answer"
    assert len(res["tools_used"]) == 1        # one batch actually ran
    assert len(llm.chat_requests) == 3        # planning + planning + final
    last = llm.chat_requests[-1]
    assert "tools" not in last                # forced final answer: no tools offered
    assert "out of tool steps" in last["messages"][-1]["content"]


def test_time_budget_forces_final():
    spec = registry.ToolSpec(name="slow", display="slow", description="d",
                             parameters={"type": "object"}, source="hub", risk="read",
                             run=lambda a: time.sleep(0.2) or "done",
                             label=lambda a: "being slow")

    class FakeBrain:
        def chat(self, settings, messages):
            FakeBrain.final_messages = messages
            return "answered from budget"

        def chat_tools(self, settings, messages, tools):
            return {"text": "", "tool_calls": [{"id": "z", "name": "slow",
                                                "arguments": "{}"}]}

    log_rows = []
    ctx = {"persona": "p", "events": EventBus(), "log": lambda **kw: log_rows.append(kw),
           "status": type("S", (), {"note_tools_worked": lambda s: None,
                                    "note_tools_rejected": lambda s, r: None})(),
           "clients": None, "deadline": time.monotonic() + 0.1}
    cfg = {"max_steps": 8, "max_seconds": 5, "permissions": {}}
    msgs = [{"role": "user", "content": "hi"}]
    out = toolloop.run_turn(FakeBrain(), {}, cfg, [spec], msgs, ctx)
    assert out["text"] == "answered from budget"
    assert out["tools_used"] == [{"tool": "slow", "label": "being slow"}]
    assert "out of tool steps or time" in FakeBrain.final_messages[-1]["content"]
    assert msgs == [{"role": "user", "content": "hi"}]  # input list never mutated
    # deadline already blown before the first call -> straight to a plain answer
    ctx2 = dict(ctx, deadline=time.monotonic() - 1, tools_used=None)
    out2 = toolloop.run_turn(FakeBrain(), {}, cfg, [spec], msgs, ctx2)
    assert out2["text"] == "answered from budget" and out2["tools_used"] == []


def test_tools_rejected_falls_back_and_reports(tmp_path, searxng):
    client, llm = llm_client(tmp_path, [{"content": "no function calling here"}],
                             searxng=searxng, reject_tools=True)
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["reply"] == "no function calling here"   # turn still answered
    status = client.get("/api/tools/status").json()
    assert status["function_calling"] == "rejected"
    assert "tools" in status["reason"]
    # every request the fake actually served was tool-free (the tools-rejecting
    # attempt never landed in chat_requests; the retry went plain)
    assert llm.chat_requests and all("tools" not in req for req in llm.chat_requests)
    llm.reject_tools = False
    llm.chat_requests.clear()
    client.post("/api/chat", json={"text": "again"})
    assert any("tools" in req for req in llm.chat_requests)  # retried with tools
    assert client.get("/api/tools/status").json()["function_calling"] == "ok"


def test_never_permission_not_offered(tmp_path, searxng):
    client, llm = llm_client(tmp_path, [{"content": "ok"}], searxng=searxng)
    client.put("/api/settings", json={"tools": {"permissions": {"get_time": "never"}}})
    client.post("/api/chat", json={"text": "hi"})
    offered = [t["function"]["name"] for t in llm.chat_requests[0]["tools"]]
    assert "get_time" not in offered and "web_search" in offered


def test_disabled_tools_plain_path(tmp_path, searxng):
    client, llm = llm_client(tmp_path, [{"content": "just talking"}], searxng=searxng)
    client.put("/api/settings", json={"tools": {"enabled": False}})
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res == {**res, "reply": "just talking", "tools_used": []}
    assert all("tools" not in req for req in llm.chat_requests)
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert "can call these tools" not in sysmsg


def test_hub_tool_ask_without_client_auto_approved(tmp_path):
    client, llm = llm_client(tmp_path, [
        call_step("get_time", {}), {"content": "done"}])
    client.put("/api/settings", json={"tools": {"permissions": {"get_time": "ask"}}})
    res = client.post("/api/chat", json={"text": "time?"}).json()
    assert res["reply"] == "done"
    log = client.get("/api/tools/log").json()["entries"]
    assert log[0]["approval"] == "auto (no client online)"


# ── event bus unit ──────────────────────────────────────────────────────────

def test_eventbus_ring_order_and_wait():
    bus = EventBus()
    for i in range(3):
        bus.publish("thing", n=i)
    got = bus.wait(0, 0.01)
    assert [e["seq"] for e in got["events"]] == [1, 2, 3]
    assert got["last"] == 3
    assert bus.wait(3, 0.01)["events"] == []

    def late():
        time.sleep(0.05)
        bus.publish("late-event")
    threading.Thread(target=late, daemon=True).start()
    got = bus.wait(3, 2.0)
    assert [e["type"] for e in got["events"]] == ["late-event"]


def test_events_route_validation(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    assert client.get("/api/events?after=x").status_code == 422
    out = client.get("/api/events?after=0&wait=0.01").json()
    assert out == {"events": [], "last": 0}
