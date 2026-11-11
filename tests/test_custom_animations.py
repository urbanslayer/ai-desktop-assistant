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


# ── web Animations tab Preview: hub 'action' event → page 'actions' event ──
# (hub/server.py publishes {persona_id, actions:[{name, at, type:'gesture',
# custom, preview, animation:{...inline...}}]}; the desktop forwards the
# actions to the page exactly like reply actions ride the 'say' event —
# inline first, then the director — but with no bubble and no voice.)

def preview_event(persona_id="riko", name="victory_dance"):
    anim = victory()
    anim["name"] = name
    return {"persona_id": persona_id, "emotion": None,
            "actions": [{"name": name, "at": 0.0, "type": "gesture",
                         "custom": True, "preview": True, "animation": anim}]}


def drain(url):
    return requests.get(f"{url}/next", timeout=5).json()["events"]


def page_events_for(url, want_type, timeout=5):
    """Poll /next until an event of `want_type` shows up; returns all seen."""
    out, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        out += drain(url)
        if any(e["type"] == want_type for e in out):
            break
        time.sleep(0.05)
    return out


def test_action_preview_for_active_persona_reaches_page(anim_env):
    cfg, url, fake_hub, notified = anim_env
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        fake_hub.push_event("action", **preview_event())
        seen = page_events_for(url, "actions")
        got = [e for e in seen if e["type"] == "actions"]
        assert len(got) == 1
        # the full action dict rides through untouched, inline animation and
        # all — the page registers it before director.say validates the name
        assert got[0]["actions"] == preview_event()["actions"]
    finally:
        s.close()


def test_action_preview_for_another_persona_is_dropped(anim_env):
    cfg, url, fake_hub, notified = anim_env
    s = session.Session(cfg, source="desktop")
    s.follow_hub_events(wait=1)
    try:
        fake_hub.push_event("action", **preview_event(persona_id="asuka"))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [e for e in drain(url) if e["type"] == "actions"]
        # positive control on the same follower: the ACTIVE persona plays
        fake_hub.push_event("action", **preview_event(persona_id="riko"))
        assert any(e["type"] == "actions"
                   for e in page_events_for(url, "actions"))
    finally:
        s.close()


def test_action_preview_redelivery_is_deduped(anim_env):
    cfg, url, fake_hub, notified = anim_env
    s = session.Session(cfg, source="desktop")
    try:
        # feed the handler directly: a ring replay re-delivers the SAME event
        # dict (same seq+time), which the follower would hand over verbatim
        ev = {"seq": 42, "time": "2026-11-03T00:00:00Z", "type": "action",
              **preview_event()}
        s._on_hub_event("action", ev)
        s._on_hub_event("action", ev)   # double delivery
        s._on_hub_event("action", ev)
        # a SECOND preview of the same animation (new seq) must still play
        ev2 = {"seq": 43, "time": "2026-11-03T00:00:01Z", "type": "action",
               **preview_event()}
        s._on_hub_event("action", ev2)
        seen = [e for e in drain(url) if e["type"] == "actions"]
        assert len(seen) == 2
    finally:
        s.close()


def test_touch_action_event_is_not_replayed(anim_env):
    # /api/touch publishes 'action' too, but the touch reply already played
    # those actions on this desktop — forwarding them would double-play.
    cfg, url, fake_hub, notified = anim_env
    s = session.Session(cfg, source="desktop")
    try:
        touch_ev = {"seq": 50, "time": "2026-11-03T00:00:02Z", "type": "action",
                    "persona_id": "riko", "emotion": "happy",
                    "touch": {"kind": "pat"},
                    "actions": [{"name": "nod", "at": 0.0, "type": "gesture"}]}
        s._on_hub_event("action", touch_ev)
        no_preview = {"seq": 51, "time": "2026-11-03T00:00:03Z", "type": "action",
                      "persona_id": "riko", "emotion": None,
                      "actions": [{"name": "nod", "at": 0.0, "type": "gesture"}]}
        s._on_hub_event("action", no_preview)
        assert [e for e in drain(url) if e["type"] == "actions"] == []
    finally:
        s.close()


# ── page-side glue (index.html inline script; assert the wiring exists) ──
INDEX_HTML = (pathlib.Path(__file__).parent.parent
              / "client" / "index.html").read_text()


def test_page_actions_event_glue_present():
    # the desktop's 'actions' event has a handler that registers the inline
    # animations BEFORE feeding director.say (the curtsey_twirl ordering)
    branch = INDEX_HTML.index("} else if (ev.type === 'actions') {")
    body = INDEX_HTML[branch:branch + 1500]
    assert "registerInlineAnimations(acts" in body
    assert body.index("registerInlineAnimations(acts") < body.index("director.say(")
    assert "actions: acts" in body  # the director schedules the very same list


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
