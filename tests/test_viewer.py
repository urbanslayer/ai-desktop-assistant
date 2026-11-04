"""Tests for riko.viewer: the local HTTP surface the avatar page and riko-ctl use.

The server runs once per test session on a random port (RIKO_PORT is set before
viewer.start(); never 8080, which the desktop app may be using). The hub-backed
bits (/avatar.vrm, /settings persona name) get a fake hub connected per test.
"""
import http.client

import pytest
import requests

from conftest import avatar_bytes, free_port
from riko import hub_client, viewer


@pytest.fixture(scope="session")
def base_url():
    import os
    port = free_port()
    os.environ["RIKO_PORT"] = str(port)
    try:
        cfg = {"desktop": {"width": 420, "height": 680, "bubble_seconds": 8}}
        server = viewer.start(cfg)
        assert viewer.PORT == port and viewer.PORT != 8080  # RIKO_PORT was honoured
        yield f"http://127.0.0.1:{port}"
        server.shutdown()
        server.server_close()
    finally:
        os.environ.pop("RIKO_PORT", None)


def drain(url):
    return requests.get(f"{url}/next", timeout=5).json()["events"]


@pytest.fixture(autouse=True)
def _clean_state(base_url):
    drain(base_url)  # start each test from an empty event queue
    yield


def test_next_drains_events(base_url):
    viewer.emit("status", state="thinking")
    viewer.emit("persona", id="asuka", name="Asuka", avatar_version=2)
    body = requests.get(f"{base_url}/next", timeout=5).json()
    assert body["audio"] is None
    assert body["events"] == [
        {"type": "status", "state": "thinking"},
        {"type": "persona", "id": "asuka", "name": "Asuka", "avatar_version": 2},
    ]
    # drained: the next poll sees nothing until something new is emitted
    assert requests.get(f"{base_url}/next", timeout=5).json()["events"] == []


def test_settings_returns_desktop_section_plus_active_persona(base_url, hub_connected):
    from riko.shortcuts import resolve
    defaults, _ = resolve(None, warn=False)
    body = requests.get(f"{base_url}/settings", timeout=5).json()
    assert body == {"width": 420, "height": 680, "bubble_seconds": 8,
                    "shortcuts": defaults, "persona_name": "Riko"}


def test_desktop_settings_resolves_and_warns(capsys):
    # overrides ride through; a Super binding is dropped with a startup warning
    settings = viewer._desktop_settings({"desktop": {"width": 1, "shortcuts": {
        "hide": "Ctrl+H", "quit": "Super+Q"}}})
    assert settings["width"] == 1
    assert settings["shortcuts"]["hide"] == ["Ctrl+H"]
    assert settings["shortcuts"]["quit"] == []  # a rejected binding does not fall back
    assert settings["shortcuts"]["talk"] == ["Space"]  # untouched actions keep defaults
    out = capsys.readouterr().out
    assert "[shortcuts]" in out and "Super" in out


def test_settings_persona_name_is_none_without_a_hub(base_url):
    assert requests.get(f"{base_url}/settings", timeout=5).json()["persona_name"] is None


def test_done_is_accepted_without_json(base_url):
    assert requests.post(f"{base_url}/done", timeout=5).status_code == 200


# ── /avatar.vrm: the hub's active persona, cached by id + version ──
def test_avatar_serves_active_persona_and_refetches_on_version_change(base_url, fake_hub,
                                                                      monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    hub_client.connect({"hub": {"mode": "remote", "url": fake_hub.url,
                                "api_key_env": "FAKE_HUB_KEY"}})

    r = requests.get(f"{base_url}/avatar.vrm", timeout=5)
    assert r.status_code == 200
    assert r.content == avatar_bytes("riko", 1)
    assert "gltf" in r.headers["Content-Type"]
    assert fake_hub.avatar_requests == [("riko", 1)]

    # same persona, same version: still the disk cache, no new hub traffic
    assert requests.get(f"{base_url}/avatar.vrm?v=riko-1", timeout=5).content == \
        avatar_bytes("riko", 1)
    assert fake_hub.avatar_requests == [("riko", 1)]

    fake_hub.bump_avatar("riko")
    assert requests.get(f"{base_url}/avatar.vrm?v=riko-2", timeout=5).content == \
        avatar_bytes("riko", 2)
    assert fake_hub.avatar_requests == [("riko", 1), ("riko", 2)]

    # and the active persona's avatar, whoever that is
    fake_hub.force_active("asuka")
    assert requests.get(f"{base_url}/avatar.vrm", timeout=5).content == avatar_bytes("asuka", 1)


def test_avatar_without_a_hub_is_a_503_error(base_url):
    r = requests.get(f"{base_url}/avatar.vrm", timeout=5)
    assert r.status_code == 503
    assert "hub" in r.json()["error"]


def test_say_requires_json_content_type(base_url):
    seen = []
    viewer.on_say(seen.append)
    try:
        r = requests.post(f"{base_url}/say", data=b'{"text": "hi"}', timeout=5)
        assert r.status_code == 415
        assert seen == []
        r = requests.post(f"{base_url}/say", json={"text": "hi senpai"}, timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert seen == ["hi senpai"]
        # empty / whitespace text is accepted but not dispatched
        assert requests.post(f"{base_url}/say", json={"text": "   "}, timeout=5).status_code == 200
        assert seen == ["hi senpai"]
        # body must be a JSON object, not a list/scalar
        r = requests.post(f"{base_url}/say", data=b"[1,2]", timeout=5,
                          headers={"Content-Type": "application/json"})
        assert r.status_code == 400
    finally:
        viewer.on_say(None)


def test_control_dispatch_and_unknown(base_url):
    calls = []

    def control(cmd, args):
        calls.append((cmd, args))
        if cmd == "forget":
            return {"ok": True, "did": "forget"}
        return None  # handlers may return nothing; the viewer turns that into {"ok": true}

    viewer.on_control(control)
    try:
        r = requests.post(f"{base_url}/control/forget", json={}, timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True, "did": "forget"}
        r = requests.post(f"{base_url}/control/whatever", json={}, timeout=5)
        assert r.json() == {"ok": True}  # unknown commands are the handler's business
        assert calls == [("forget", {}), ("whatever", {})]
    finally:
        viewer.on_control(None)


def test_control_persona_passes_the_body(base_url):
    calls = []
    viewer.on_control(lambda cmd, args: calls.append((cmd, args)) or None)
    try:
        r = requests.post(f"{base_url}/control/persona", json={"name": "asuka"}, timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert calls == [("persona", {"name": "asuka"})]
    finally:
        viewer.on_control(None)


def test_control_handler_exception_returns_500(base_url):
    def boom(cmd, args):
        raise ValueError("handler exploded")

    viewer.on_control(boom)
    try:
        r = requests.post(f"{base_url}/control/talk", json={}, timeout=5)
        assert r.status_code == 500
        assert "handler exploded" in r.json()["error"]
    finally:
        viewer.on_control(None)


def test_bad_json_is_400(base_url):
    r = requests.post(f"{base_url}/say", data=b"{oops", timeout=5,
                      headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_chunked_post_body(base_url):
    seen = []
    port = int(base_url.rsplit(":", 1)[1])  # base_url, not viewer.PORT: another fixture may have moved it
    viewer.on_say(seen.append)
    try:
        payload = b'{"text": "chunked"}'
        half = len(payload) // 2
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/say")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        # two chunks + terminating zero-length chunk
        conn.send(f"{half:x}\r\n".encode() + payload[:half] + b"\r\n")
        rest = payload[half:]
        conn.send(f"{len(rest):x}\r\n".encode() + rest + b"\r\n0\r\n\r\n")
        assert conn.getresponse().status == 200
        conn.close()
        assert seen == ["chunked"]
    finally:
        viewer.on_say(None)


def test_animations_json_lists_dropped_vrma_files(base_url, tmp_path, monkeypatch):
    anims = tmp_path / "client" / "animations"
    anims.mkdir(parents=True)
    for name in ("wave.vrma", "think.vrma", "_draft.vrma", "README.md"):
        (anims / name).write_bytes(b"glTF")
    monkeypatch.setattr(viewer, "ROOT", tmp_path)
    body = requests.get(f"{base_url}/animations.json", timeout=5).json()
    names = sorted(a["name"] for a in body["animations"])
    assert names == ["think", "wave"], body  # underscore and non-vrma ignored
    urls = {a["name"]: a["url"] for a in body["animations"]}
    assert urls["wave"] == "animations/wave.vrma"


def test_animations_json_is_empty_when_folder_missing(base_url, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, "ROOT", tmp_path)  # tmp_path/client/animations does not exist
    body = requests.get(f"{base_url}/animations.json", timeout=5).json()
    assert body == {"animations": []}


def test_index_page_is_served(base_url):
    r = requests.get(base_url + "/", timeout=5)
    assert r.status_code == 200
    assert "html" in r.headers["Content-Type"].lower()


def test_heard_and_say_events_carry_their_text(base_url):
    # the director watches these payloads to spot greetings (2026-11 owner ask),
    # so the text must survive the trip through the event queue untouched
    viewer.emit("heard", text="Good morning, Riko!")
    viewer.emit("say", text="Hi there, senpai!", emotion="happy")
    events = drain(base_url)
    heard = next(e for e in events if e["type"] == "heard")
    say = next(e for e in events if e["type"] == "say")
    assert heard == {"type": "heard", "text": "Good morning, Riko!"}
    assert say["text"] == "Hi there, senpai!" and say["emotion"] == "happy"


def test_wave_is_a_page_control_command():
    # riko-ctl wave / desktop W shortcut route through desktop.py's bridge
    import desktop
    assert "wave" in desktop.PAGE_CMDS
    assert "requestWave" in desktop.WAVE_JS
