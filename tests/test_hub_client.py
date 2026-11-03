"""riko/hub_client: the HTTP wrapper every client uses, tested against the fake hub.

Also covers startup check() in remote and local mode (fake create_app stubs,
so it passes whether or not the real hub/ package exists here).
"""
import json
import sys
import types

import pytest

from conftest import CHAT_REPLY, HUB_KEY, FakeHub, free_port, make_cfg
from riko import hub_client


def client_for(fake_hub, key=HUB_KEY):
    return hub_client.HubClient(fake_hub.url, key)


# ── the client ───────────────────────────────────────────────
def test_bearer_key_is_sent(fake_hub, tmp_path):
    client = client_for(fake_hub)
    client.state()
    assert fake_hub.last_auth == f"Bearer {HUB_KEY}"

    open_hub = FakeHub(api_key=None)
    try:
        hub_client.HubClient(open_hub.url, "").state()
        assert open_hub.last_auth is None  # no key configured -> no header, no problem
    finally:
        open_hub.stop()


def test_key_comes_from_the_env_var_named_in_the_config(fake_hub, monkeypatch):
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    client = hub_client.connect(make_cfg(fake_hub.url))
    assert client.state()["active"]["id"] == "riko"  # would be a 401 without the key


def test_401_is_a_clear_auth_error(fake_hub):
    client = client_for(fake_hub, key="wrong-key")
    with pytest.raises(hub_client.HubAuthError) as e:
        client.state()
    assert "401" in str(e.value) and fake_hub.url in str(e.value)


def test_down_hub_is_hub_unreachable():
    hub = FakeHub()
    url = hub.url
    hub.stop()
    client = hub_client.HubClient(url, HUB_KEY)
    with pytest.raises(hub_client.HubUnreachable) as e:
        client.state()
    assert url in str(e.value)


def test_hub_error_carries_the_detail(fake_hub):
    client = client_for(fake_hub)
    with pytest.raises(hub_client.HubError) as e:
        client.set_active("nobody")
    assert "404" in str(e.value) and "no such persona" in str(e.value)


def test_chat_result_handling_and_timeouts(fake_hub):
    assert hub_client.CHAT_TIMEOUT >= 180  # reasoning models are slow
    client = client_for(fake_hub)
    result = client.chat("hello", source="terminal")
    assert result["switched"] is False and result["persona"] == "riko"
    assert "you said: hello" in result["reply"]
    assert fake_hub.chat_requests[-1] == {"text": "hello", "source": "terminal"}

    switched = client.chat("switch to asuka", source="desktop")
    assert switched["switched"] is True and switched["persona"] == "asuka"
    assert switched["reply"] == "Asuka here, senpai."  # greeting, not an LLM reply
    assert switched["message_ids"] == []  # a switch is not stored in history


def test_history_and_clear(fake_hub):
    client = client_for(fake_hub)
    client.chat("remember me", source="terminal")
    contents = [m["content"] for m in client.history()["messages"]]
    assert contents == ["remember me", CHAT_REPLY.format(name="Riko", text="remember me")]
    client.clear_history()
    assert client.history()["messages"] == []


def test_avatar_is_cached_and_refetched_on_new_version(fake_hub, tmp_path):
    client = hub_client.HubClient(fake_hub.url, HUB_KEY, cache_dir=tmp_path)
    path = client.avatar_file("riko", 1)
    again = client.avatar_file("riko", 1)
    assert path == again and path.read_bytes().startswith(b"glTF")
    assert fake_hub.avatar_requests == [("riko", 1)]  # second call came from the cache

    fake_hub.bump_avatar("riko")
    fresh = client.avatar_file("riko", 2)
    assert fresh != path and b"v2" in fresh.read_bytes()
    assert fake_hub.avatar_requests == [("riko", 1), ("riko", 2)]


# ── startup check(), remote mode ─────────────────────────────
def test_check_remote_happy_path(fake_hub, monkeypatch):
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    client = hub_client.check(make_cfg(fake_hub.url))
    assert client.url == fake_hub.url
    assert hub_client.active_persona()["id"] == "riko"


def test_check_remote_unreachable_exits_with_hint(monkeypatch):
    cfg = make_cfg(f"http://127.0.0.1:{free_port()}")  # nothing listening
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    with pytest.raises(SystemExit) as e:
        hub_client.check(cfg)
    assert "docker compose up" in str(e.value)


def test_check_remote_wrong_key_exits_with_key_hint(fake_hub, monkeypatch):
    monkeypatch.setenv("FAKE_HUB_KEY", "not-the-key")
    with pytest.raises(SystemExit) as e:
        hub_client.check(make_cfg(fake_hub.url))
    assert "RIKO_HUB_KEY" in str(e.value) or "FAKE_HUB_KEY" in str(e.value)


# ── startup check(), local mode ──────────────────────────────
def _local_cfg(port):
    return {"hub": {"mode": "local", "api_key_env": "", "local_port": port,
                    "local_data": "hub_data"},
            "llm": {"base_url": "http://127.0.0.1:1/v1", "api_key_env": ""}}


def test_check_local_without_hub_package_exits_clearly(monkeypatch):
    monkeypatch.setitem(sys.modules, "hub", None)  # `import hub` fails no matter what
    with pytest.raises(SystemExit) as e:
        hub_client.check(_local_cfg(free_port()))
    msg = str(e.value)
    assert "hub" in msg and "remote" in msg  # says what's missing AND the way out


def test_check_local_starts_the_hub_in_process(monkeypatch, tmp_path):
    """With a stand-in create_app, local mode must run uvicorn and wait until it answers."""
    started = {}

    async def app(scope, receive, send):
        path = scope.get("path", "")
        payload = ({"version": 1, "active": {"id": "riko", "name": "Riko", "avatar_version": 1},
                    "personas": [{"id": "riko", "name": "Riko"}],
                    "last_message_id": 0, "llm_model": "test-model"}
                   if path == "/api/state" else {"ok": True, "version": "1"})
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def create_app(data_dir, api_key=None, llm_defaults=None):
        started["data_dir"] = data_dir
        started["api_key"] = api_key
        started["llm_defaults"] = llm_defaults
        return app

    hub_mod = types.ModuleType("hub")
    server_mod = types.ModuleType("hub.server")
    server_mod.create_app = create_app
    hub_mod.server = server_mod
    monkeypatch.setitem(sys.modules, "hub", hub_mod)
    monkeypatch.setitem(sys.modules, "hub.server", server_mod)
    monkeypatch.setattr(hub_client, "ROOT", tmp_path)

    port = free_port()
    client = hub_client.check(_local_cfg(port))
    assert client.url == f"http://127.0.0.1:{port}"
    assert str(started["data_dir"]) == str(tmp_path / "hub_data")
    assert started["llm_defaults"]["base_url"] == "http://127.0.0.1:1/v1"


def test_check_local_busy_port_exits_with_hint(monkeypatch):
    blocker = FakeHub()  # anything already holding a port
    port = int(blocker.url.rsplit(":", 1)[1])
    hub_mod = types.ModuleType("hub")
    server_mod = types.ModuleType("hub.server")
    server_mod.create_app = lambda data_dir, api_key=None, llm_defaults=None: None
    hub_mod.server = server_mod
    monkeypatch.setitem(sys.modules, "hub", hub_mod)
    monkeypatch.setitem(sys.modules, "hub.server", server_mod)
    try:
        with pytest.raises(SystemExit) as e:
            hub_client.check(_local_cfg(port))
        assert "busy" in str(e.value)
    finally:
        blocker.stop()
