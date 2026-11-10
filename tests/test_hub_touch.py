"""POST /api/touch (docs/TOUCH.md) against the hub TestClient + FakeHubLLM.

The heart of this file is the *fast path* proof: for mode off, cooldown,
the 10-minute limit, busy and always-silent kinds the fake LLM must not
have received a single chat request. Then: the spoken round trip, the
history note shape, and the settings round-trip.
"""
import pytest
from fastapi.testclient import TestClient

from hub.server import create_app
from test_hub_brain import FakeHubLLM
from test_hub_api import VRM_BYTES, add_persona, make_client  # noqa: F401


@pytest.fixture
def fake_llm():
    server = FakeHubLLM()
    yield server
    server.stop()


@pytest.fixture
def client(tmp_path, fake_llm):
    return make_client(tmp_path, llm_defaults={"base_url": fake_llm.base_url,
                                               "model": "auto", "max_tokens": 1024})


def touch(client, kind, **kw):
    body = {"kind": kind, "source": "desktop"}
    body.update(kw)
    return client.post("/api/touch", json=body)


def touch_settings(client):
    return client.get("/api/settings").json()["touch"]


# ── contract errors ────────────────────────────────────────────────────────

def test_unknown_kind_400(client, fake_llm):
    r = touch(client, "boop")
    assert r.status_code == 400
    assert r.json() == {"detail": "unknown touch kind: boop"}
    assert fake_llm.chat_requests == []


def test_unknown_persona_404(client):
    r = touch(client, "pat", persona="nobody")
    assert r.status_code == 404
    assert r.json() == {"detail": "no persona 'nobody'"}


def test_bad_body_400(client):
    assert client.post("/api/touch", json=["not", "a", "dict"]).status_code == 400


# ── settings defaults + round-trip ─────────────────────────────────────────

def test_touch_settings_defaults_and_round_trip(client):
    st = client.get("/api/settings").json()
    # docs/TOUCH.md defaults: quiet -> 45 s / 4 per 10 min
    assert st["touch"] == {"mode": "quiet", "cooldown_s": 45, "max_per_10min": 4}
    assert all(v == "default" for v in st["touch_source"].values())
    assert st["defaults"]["touch"] == st["touch"]

    # chatty with NO saved numbers gets the chatty defaults (15 s / 12)
    r = client.put("/api/settings", json={"touch": {"mode": "chatty"}})
    assert r.json()["touch"] == {"mode": "chatty", "cooldown_s": 15,
                                 "max_per_10min": 12}
    assert r.json()["touch_source"]["mode"] == "saved"

    r = client.put("/api/settings", json={"touch": {"mode": "chatty",
                                                    "cooldown_s": 2.5,
                                                    "max_per_10min": 2}})
    assert r.status_code == 200
    assert r.json()["touch"] == {"mode": "chatty", "cooldown_s": 2.5,
                                 "max_per_10min": 2}
    assert all(v == "saved" for v in r.json()["touch_source"].values())
    # reset returns to defaults
    r = client.post("/api/settings/reset", json={"touch": True})
    assert r.json()["touch"] == {"mode": "quiet", "cooldown_s": 45,
                                 "max_per_10min": 4}


def test_touch_settings_validation(client):
    assert client.put("/api/settings", json={"touch": {"mode": "loud"}}).status_code == 422
    assert client.put("/api/settings", json={"touch": {"cooldown_s": -1}}).status_code == 422
    assert client.put("/api/settings", json={"touch": {"max_per_10min": "6"}}).status_code == 422
    assert client.put("/api/settings", json={"touch": 7}).status_code == 422


# ── fast paths: NO LLM call is made ────────────────────────────────────────

def test_mode_off_is_fast_and_silent(client, fake_llm):
    client.put("/api/settings", json={"touch": {"mode": "off"}})
    for kind, kw in [("pat", {}), ("tickle", {}), ("release", {"held_ms": 9000})]:
        r = touch(client, kind, **kw).json()
        assert r == {"ok": True, "reacted": False, "spoke": False, "text": "",
                     "emotion": "neutral", "actions": [], "audio_url": None,
                     "reason": "off", "detail": "mode_off",
                     "state_version": r["state_version"]}
    assert fake_llm.chat_requests == []


def test_grab_and_action_never_speak_even_chatty(client, fake_llm):
    client.put("/api/settings", json={"touch": {"mode": "chatty",
                                                "cooldown_s": 0}})
    for kind in ("grab", "action", "sit", "stand"):
        r = touch(client, kind).json()
        assert r["spoke"] is False and r["detail"] == "silent_kind"
    # chatty: poke DOES speak (every kind but grab/action/sit/stand)
    fake_llm.reply = "[surprise] Eek!"
    r = touch(client, "poke").json()
    assert r["reason"] == "llm" and r["spoke"] is True
    assert len(fake_llm.chat_requests) == 1


def test_quiet_speaks_only_pat_tickle_and_long_release(client, fake_llm):
    client.put("/api/settings", json={"touch": {"cooldown_s": 0}})
    r = touch(client, "release", held_ms=1000).json()   # short release: quiet -> silent
    assert r["reason"] == "quiet" and r["detail"] == "silent_kind"
    r = touch(client, "stand").json()                   # sit/stand never speak
    assert r["reason"] == "quiet" and r["detail"] == "silent_kind"
    r = touch(client, "poke").json()                    # quiet: poke stays page-local
    assert r["reason"] == "quiet"
    r = touch(client, "release", held_ms=3000).json()   # > 3 s, strictly
    assert r["reason"] == "quiet"
    assert fake_llm.chat_requests == []
    fake_llm.reply = "[happy] Finally, hands off!"
    r = touch(client, "release", held_ms=3001).json()
    assert r["reason"] == "llm" and r["spoke"] is True
    assert len(fake_llm.chat_requests) == 1


def test_cooldown_fast_path(client, fake_llm):
    fake_llm.reply = "[happy] Hey hey!"
    client.put("/api/settings", json={"touch": {"mode": "chatty",
                                                "cooldown_s": 60}})
    assert touch(client, "pat").json()["reason"] == "llm"
    n = len(fake_llm.chat_requests)
    r = touch(client, "pat").json()                     # 60 s cooldown, now
    assert r["reason"] == "cooldown" and r["reacted"] is False
    assert len(fake_llm.chat_requests) == n             # no second LLM call
    # silent kinds never reset or ride the cooldown: grab stays silent_kind
    assert touch(client, "grab").json()["reason"] == "silent_kind"


def test_rate_limit_window(client, fake_llm):
    fake_llm.reply = "[happy] Mrrp!"
    client.put("/api/settings", json={"touch": {"mode": "chatty",
                                                "cooldown_s": 0,
                                                "max_per_10min": 2}})
    assert touch(client, "pat").json()["reason"] == "llm"
    assert touch(client, "pat").json()["reason"] == "llm"
    r = touch(client, "tickle").json()
    assert r["reason"] == "limit" and r["spoke"] is False
    n = len(fake_llm.chat_requests)
    touch(client, "pat")
    assert len(fake_llm.chat_requests) == n             # capped: no LLM
    # max 0 = never speak
    client.put("/api/settings", json={"touch": {"max_per_10min": 0}})
    assert touch(client, "pat").json()["reason"] == "limit"


def test_busy_fast_path(client, fake_llm):
    lock = client.app.state.persona_lock_for("riko")
    assert lock.acquire(blocking=False)
    try:
        r = touch(client, "pat").json()
        assert r["reason"] == "busy" and r["reacted"] is False
    finally:
        lock.release()
    assert fake_llm.chat_requests == []


def test_llm_failure_is_silent(client, fake_llm):
    fake_llm.fail = True
    r = touch(client, "pat").json()
    assert r["spoke"] is False and r["reason"] == "llm_error"


# ── the real spoken round trip ─────────────────────────────────────────────

def test_spoken_round_trip_history_and_prompt(client, fake_llm):
    fake_llm.reply = "[happy] Senpai! Careful with the hair!"
    r = touch(client, "pat").json()
    assert r["ok"] and r["reacted"] and r["spoke"] and r["reason"] == "llm"
    assert r["text"] == "Senpai! Careful with the hair!"
    assert r["emotion"] == "happy" and r["actions"] == []
    assert r["audio_url"] is None          # tts off by default (no base_url)

    # the model got the touch as the LAST user turn, no tools
    req = fake_llm.chat_requests[-1]
    assert "tools" not in req or not req.get("tools")
    msgs = req["messages"]
    assert msgs[-1]["role"] == "user"
    assert "patted your head" in msgs[-1]["content"]
    assert "ONE short" in msgs[0]["content"]

    # history: grey system note + assistant reaction, NO fake user message
    hist = client.get("/api/history?persona=riko").json()["messages"]
    notes = [m for m in hist if m["role"] == "system"]
    assert len(notes) == 1
    assert notes[0]["content"] == "owner touched: patted her head (quiet)"
    assert notes[0]["source"] == "touch"
    assert notes[0]["meta"]["touch"]["kind"] == "pat"
    replies = [m for m in hist if m["role"] == "assistant" and m["source"] == "touch"]
    assert len(replies) == 1
    assert replies[0]["content"] == "Senpai! Careful with the hair!"
    assert replies[0]["emotion"] == "happy"
    assert replies[0]["meta"]["touch"]["kind"] == "pat"
    assert not [m for m in hist if m["role"] == "user"]


def test_silent_touch_still_lands_in_history(client, fake_llm):
    client.put("/api/settings", json={"touch": {"mode": "off"}})
    touch(client, "tickle")
    hist = client.get("/api/history?persona=riko").json()["messages"]
    assert [m["role"] for m in hist] == ["system", "assistant"]
    assert hist[0]["content"] == "owner touched: tickled her (off)"
    assert hist[1]["meta"]["touch"]["silent"] is True
    assert hist[1]["meta"]["touch"]["reason"] == "mode_off"
    assert hist[0]["meta"]["touch"]["note"] is True
    assert fake_llm.chat_requests == []


def test_touch_uses_named_persona_not_active(client, fake_llm):
    add_persona(client, "Asuka")
    fake_llm.reply = "Hmph!"
    r = touch(client, "pat", persona="asuka")
    # reaction stored on asuka, chat stays on riko
    hist = client.get("/api/history?persona=asuka").json()["messages"]
    assert any(m["role"] == "system" for m in hist)
    assert not client.get("/api/history?persona=riko").json()["messages"]


# ── auth (same bearer rule as every other route) ──────────────────────────

def test_touch_requires_key(tmp_path):
    client = make_client(tmp_path, api_key="secret", default_auth=False)
    assert client.post("/api/touch", json={"kind": "pat"}).status_code == 401
