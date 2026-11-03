"""Tests for riko.viewer: the local HTTP surface the avatar page and riko-ctl use.

The server runs once per test session on a random port (RIKO_PORT is set before
viewer.start(); never 8080, which the desktop app may be using).
"""
import http.client
import os
import urllib.request

import pytest
import requests

from conftest import free_port
from riko import viewer


@pytest.fixture(scope="session")
def base_url():
    port = free_port()
    os.environ["RIKO_PORT"] = str(port)
    try:
        cfg = {
            "vrm_model": "character_files/sample_avatar.vrm",
            "history_file": "chat_history.json",
            "desktop": {"width": 420, "height": 680, "bubble_seconds": 8},
        }
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
    viewer.emit("heard", text="hello")
    body = requests.get(f"{base_url}/next", timeout=5).json()
    assert body["audio"] is None
    assert body["events"] == [
        {"type": "status", "state": "thinking"},
        {"type": "heard", "text": "hello"},
    ]
    # drained: the next poll sees nothing until something new is emitted
    assert requests.get(f"{base_url}/next", timeout=5).json()["events"] == []


def test_settings_returns_desktop_section(base_url):
    assert requests.get(f"{base_url}/settings", timeout=5).json() == {
        "width": 420, "height": 680, "bubble_seconds": 8}


def test_done_is_accepted_without_json(base_url):
    assert requests.post(f"{base_url}/done", timeout=5).status_code == 200


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

    def control(cmd):
        calls.append(cmd)
        if cmd == "forget":
            return {"ok": True, "did": "forget"}
        return None  # handlers may return nothing; the viewer turns that into {"ok": true}

    viewer.on_control(control)
    try:
        r = requests.post(f"{base_url}/control/forget", json={}, timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True, "did": "forget"}
        r = requests.post(f"{base_url}/control/whatever", json={}, timeout=5)
        assert r.json() == {"ok": True}  # unknown commands are the handler's business
        assert calls == ["forget", "whatever"]
    finally:
        viewer.on_control(None)


def test_control_handler_exception_returns_500(base_url):
    def boom(cmd):
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


def test_index_page_is_served(base_url):
    r = requests.get(base_url + "/", timeout=5)
    assert r.status_code == 200
    assert "html" in r.headers["Content-Type"].lower()
