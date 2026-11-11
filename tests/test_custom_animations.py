"""Custom keyframe animations on the desktop side (docs/SELF_ANIMATION.md).

The desktop is the cache between the hub and the avatar page:
  - fetch GET /api/personas/{id}/animations?model=current&status=all at
    startup (helper thread), on persona switches and on the hub event
    `animations_changed` (tested against FakeHub, the hub side is A6-hub's);
  - the viewer serves the cache at GET /custom-animations.json;
  - every successful refresh pings the page through `custom-animations:reload`
    (the Qt bridge glue lives in desktop.py; here we test the callback that
    feeds it, never the Qt thread itself).
"""
import json
import pathlib
import time

import pytest
import requests

from conftest import free_port, make_cfg
from riko import hub_client, session, viewer

FIXTURE = (pathlib.Path(__file__).parent.parent
           / "client" / "js" / "fixtures" / "animation_victory.json")


@pytest.fixture
def anim_env(monkeypatch, fake_hub, tmp_path):
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", "test-hub-key")
    cfg = make_cfg(fake_hub.url)
    server = viewer.start(cfg)
    hub_client.connect(cfg)
    hub_client.note_active(fake_hub.state())  # startup knows the active persona
    notified = []
    hub_client.set_on_custom_animations(notified.append)
    url = f"http://127.0.0.1:{port}"
    requests.get(f"{url}/next", timeout=5)
    yield cfg, url, fake_hub, notified
    server.shutdown()
    server.server_close()


def victory():
    return json.loads(FIXTURE.read_text())


def test_refresh_fetches_contract_url_and_viewer_serves_cache(anim_env):
    cfg, url, fake_hub, notified = anim_env
    fake_hub.animations["riko"] = [victory()]
    assert hub_client.refresh_custom_animations() is True
    # the literal contract URL, query included
    assert fake_hub.animation_requests == [("riko", "current", "all")]
    # the page's only view of the list
    served = requests.get(f"{url}/custom-animations.json", timeout=5).json()
    assert served == {"animations": [victory()]}
    # and the page gets pinged to re-read it
    assert notified == [1]


def test_hub_without_the_endpoint_keeps_cache_empty(anim_env):
    cfg, url, fake_hub, notified = anim_env
    fake_hub.has_animations = False  # an old hub: 404 on the list
    assert hub_client.refresh_custom_animations() is False
    assert hub_client.custom_animations() == []
    assert requests.get(f"{url}/custom-animations.json", timeout=5).json() \
        == {"animations": []}
    assert notified == []


def test_failed_refresh_keeps_previous_cache(anim_env):
    cfg, url, fake_hub, notified = anim_env
    fake_hub.animations["riko"] = [victory()]
    assert hub_client.refresh_custom_animations() is True
    fake_hub.has_animations = False  # hub updated out from under us / went down
    assert hub_client.refresh_custom_animations() is False
    assert hub_client.custom_animations() == [victory()]  # kept, not wiped
    assert requests.get(f"{url}/custom-animations.json", timeout=5).json() \
        == {"animations": [victory()]}


def test_persona_switch_refetches(anim_env):
    cfg, url, fake_hub, notified = anim_env
    fake_hub.animations["riko"] = [victory()]
    fake_hub.animations["asuka"] = []
    assert hub_client.refresh_custom_animations() is True
    # switch through the same announce_persona the poller/switch path uses
    state = dict(fake_hub.state(), active=fake_hub.personas["asuka"])
    assert hub_client.announce_persona(state) is True
    assert ("asuka", "current", "all") in fake_hub.animation_requests
    assert requests.get(f"{url}/custom-animations.json", timeout=5).json() \
        == {"animations": []}


def test_animations_changed_event_refetches_active_persona(anim_env):
    cfg, url, fake_hub, notified = anim_env
    fake_hub.animations["riko"] = [victory()]
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        fake_hub.push_event("animations_changed", persona_id="riko", name="victory_dance")

        def served():
            return requests.get(f"{url}/custom-animations.json", timeout=5).json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if served()["animations"]:
                break
            time.sleep(0.05)
        assert served() == {"animations": [victory()]}
        assert notified == [1]  # the page would be told to reload (Qt bridge)
    finally:
        s.close()


def test_event_for_another_persona_does_not_refetch(anim_env):
    cfg, url, fake_hub, notified = anim_env
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        fake_hub.push_event("animations_changed", persona_id="asuka", name="x")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
        assert fake_hub.animation_requests == []  # no fetch for a bystander
    finally:
        s.close()


# ── desktop.py glue (Qt side; assert the wiring exists, offscreen-safe) ──
DESKTOP = (pathlib.Path(__file__).parent.parent / "desktop.py").read_text()


def test_desktop_glue_present():
    # hub notify (hub thread) → Qt bridge command → page reload command
    assert 'hub_client.set_on_custom_animations(' in DESKTOP
    assert 'bridge.command.emit("custom-animations:reload")' in DESKTOP
    # Qt-side branch forwards it into the page like sit:/touch-mode:
    assert 'elif cmd == "custom-animations:reload"' in DESKTOP
    # startup kick runs off the Qt thread
    assert 'name="riko-anims"' in DESKTOP
