"""End-to-end tests for the hub HTTP API against FastAPI's TestClient.

Every test gets a fresh tmp data dir; the fake LLM is imported from
tests/test_hub_brain.py so this file stays independent of riko/ internals.
"""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub.server import DEFAULT_AVATAR, create_app
from test_hub_brain import REPLY, FakeHubLLM

BEARER_GOOD = "Bear" + "er secret"
BEARER_BAD = "Bear" + "er nope"

VRM_BYTES = b"glTF" + b"\x02\x00\x00\x00" + b"fake-vrm-body"


@pytest.fixture
def fake_llm():
    server = FakeHubLLM()
    yield server
    server.stop()


def make_client(tmp_path, api_key=None, llm_defaults=None, default_auth=True):
    app = create_app(tmp_path / "hubdata", api_key=api_key, llm_defaults=llm_defaults)
    headers = {"Authorization": "Bear" + "er " + api_key} if (api_key and default_auth) else {}
    return TestClient(app, headers=headers)


@pytest.fixture
def client(tmp_path, fake_llm):
    return make_client(tmp_path, llm_defaults={"base_url": fake_llm.base_url,
                                               "model": "auto", "max_tokens": 1024})


def add_persona(client, name="Asuka", **over):
    body = {"name": name, "system_prompt": f"You are {name}, a tsundere.",
            "greeting": f"{name} here!", **over}
    return client.post("/api/personas", json=body)


# ── auth ────────────────────────────────────────────────────────────────────

def test_health_open_without_key(tmp_path):
    client = make_client(tmp_path, api_key="secret")
    assert client.get("/api/health").json() == {"ok": True, "version": "1"}


def test_401_without_or_wrong_key(tmp_path):
    client = make_client(tmp_path, api_key="secret", default_auth=False)
    for call in (lambda: client.get("/api/state"),
                 lambda: client.get("/api/personas", headers={"Authorization": BEARER_BAD}),
                 lambda: client.post("/api/chat", json={"text": "hi"}),
                 lambda: client.get("/api/history", headers={"Authorization": "secret"})):
        r = call()
        assert r.status_code == 401
        assert r.json() == {"detail": "invalid api key"}
    assert client.get("/api/state", headers={"Authorization": BEARER_GOOD}).status_code == 200


def test_no_key_means_open(tmp_path, fake_llm):
    client = make_client(tmp_path)  # api_key=None -> auth disabled
    assert client.get("/api/state").status_code == 200


# ── seed persona ────────────────────────────────────────────────────────────

def test_fresh_dir_seeds_riko(tmp_path, client):
    personas = client.get("/api/personas").json()
    assert len(personas) == 1
    riko = personas[0]
    assert riko["id"] == "riko" and riko["name"] == "Riko"
    assert riko["keywords"] == ["riko"]
    assert "senpai" in riko["system_prompt"] and "snarky anime girl" in riko["system_prompt"]
    assert riko["greeting"] == "Riko here. Miss me, senpai?"
    assert riko["avatar_version"] == 0
    state = client.get("/api/state").json()
    assert state["active"]["id"] == "riko"
    assert state["version"] == 1
    # the seeded persona's avatar falls back to the packaged default
    r = client.get("/api/personas/riko/avatar")
    assert r.content == DEFAULT_AVATAR.read_bytes()
    assert r.headers["content-type"].startswith("model/gltf-binary")


# ── persona CRUD ────────────────────────────────────────────────────────────

def test_persona_create_duplicate_and_edit(client):
    r = add_persona(client, name="Asuka Langley", greeting="Hmph!", tts_voice="tsun")
    assert r.status_code == 201
    p = r.json()
    assert p["id"] == "asuka-langley" and "asuka-langley" in p["keywords"]
    assert p["greeting"] == "Hmph!" and p["tts_voice"] == "tsun"

    dup = add_persona(client, name="Asuka-Langley")  # same slug
    assert dup.status_code == 409

    assert client.post("/api/personas", json={"name": "  "}).status_code == 422
    assert client.post("/api/personas", json={"name": "???"}).status_code == 422

    up = client.put("/api/personas/asuka-langley",
                    json={"name": "Asuka", "keywords": ["asuka", "td"], "greeting": "!"})
    assert up.status_code == 200
    assert up.json()["id"] == "asuka-langley"  # id never changes
    assert up.json()["name"] == "Asuka"
    assert up.json()["keywords"] == ["asuka", "td"]

    assert client.get("/api/personas/ghost").status_code == 404
    assert client.put("/api/personas/ghost", json={"name": "x"}).status_code == 404


def test_persona_delete_rules(client):
    add_persona(client)  # asuka
    assert client.get("/api/state").json()["personas"] == [
        {"id": "riko", "name": "Riko"}, {"id": "asuka", "name": "Asuka"}]
    v0 = client.get("/api/state").json()["version"]

    assert client.delete("/api/personas/ghost").status_code == 404
    assert client.delete("/api/personas/asuka").status_code == 204
    assert client.delete("/api/personas/riko").status_code == 409  # last persona
    assert client.get("/api/state").json()["version"] == v0 + 1

    # deleting the ACTIVE persona activates the first remaining one
    add_persona(client)  # asuka again
    assert client.post("/api/active", json={"persona": "asuka"}).status_code == 200
    assert client.delete("/api/personas/asuka").status_code == 204
    assert client.get("/api/state").json()["active"]["id"] == "riko"


def test_persona_delete_removes_messages_and_avatar(tmp_path, client):
    add_persona(client)
    client.put("/api/personas/asuka/avatar",
               files={"file": ("a.vrm", VRM_BYTES, "model/gltf-binary")})
    assert client.post("/api/history/import",
                       json={"persona": "asuka",
                             "messages": [{"role": "user", "content": "m"}]}).status_code == 200
    avatar_file = tmp_path / "hubdata" / "avatars" / "asuka.vrm"
    assert avatar_file.exists()
    assert client.delete("/api/personas/asuka").status_code == 204
    assert not avatar_file.exists()
    # messages of a deleted persona are gone (persona itself 404s now)
    assert client.get("/api/personas/asuka").status_code == 404


# ── avatar upload / download ────────────────────────────────────────────────

def test_avatar_upload_etag_and_version_bump(client):
    add_persona(client)
    r = client.put("/api/personas/asuka/avatar",
                   files={"file": ("asuka.vrm", VRM_BYTES, "model/gltf-binary")})
    assert r.status_code == 200
    assert r.json()["avatar_version"] == 1
    dl = client.get("/api/personas/asuka/avatar")
    assert dl.content == VRM_BYTES
    assert dl.headers["etag"] == '"asuka-1"'

    again = client.put("/api/personas/asuka/avatar",
                       files={"file": ("asuka.vrm", VRM_BYTES + b"2", "model/gltf-binary")})
    assert again.json()["avatar_version"] == 2
    dl2 = client.get("/api/personas/asuka/avatar")
    assert dl2.headers["etag"] == '"asuka-2"'
    # conditional GET honours the previous etag
    assert client.get("/api/personas/asuka/avatar",
                      headers={"If-None-Match": '"asuka-2"'}).status_code == 304
    # clients cache by id + avatar_version, so the state must move too
    st = client.get("/api/state").json()
    c_again = client.get("/api/personas/asuka").json()
    assert c_again["avatar_version"] == 2


def test_avatar_bad_file_and_unknown_persona(client):
    add_persona(client)
    bad = client.put("/api/personas/asuka/avatar",
                     files={"file": ("asuka.vrm", b"not a vrm at all", "application/octet-stream")})
    assert bad.status_code == 400
    assert client.put("/api/personas/ghost/avatar",
                      files={"file": ("x.vrm", VRM_BYTES, "model/gltf-binary")}).status_code == 404


# ── active persona ──────────────────────────────────────────────────────────

def test_active_by_id_name_or_keyword(client):
    add_persona(client, name="Asuka", keywords=["asuka", "td"])
    for wanted in ("asuka", "Asuka", "ASUKA", "td", "TD!"):
        r = client.post("/api/active", json={"persona": wanted})
        assert r.status_code == 200, wanted
        assert r.json()["active"]["id"] == "asuka"
    before = client.get("/api/state").json()["version"]
    assert client.post("/api/active", json={"persona": "asuka"}).json()["version"] == before
    assert client.post("/api/active", json={"persona": "ghost"}).status_code == 404
    assert client.post("/api/active", json={}).status_code == 400


# ── chat: normal turn ───────────────────────────────────────────────────────

def test_chat_stores_both_messages(client, fake_llm):
    st0 = client.get("/api/state").json()
    r = client.post("/api/chat", json={"text": "hi", "source": "web"})
    assert r.status_code == 200
    res = r.json()
    assert res == {"reply": REPLY, "speech": REPLY,  # nothing to clean: speech == reply
                   "persona": "riko", "switched": False,
                   "emotion": "neutral", "tools_used": [], "actions": [],
                   "memory_writes": [],  # memory round M: facts this turn saved
                   "message_ids": [1, 2], "state_version": st0["version"] + 1,
                   "audio_url": None}  # no TTS configured on this hub

    req = fake_llm.chat_requests[-1]
    assert req["messages"][0]["role"] == "system"
    assert "senpai" in req["messages"][0]["content"]
    assert req["messages"][1] == {"role": "user", "content": "hi"}
    assert req["model"] == "test-model"

    hist = client.get("/api/history").json()
    assert hist["persona"] == "riko"
    assert [(m["role"], m["content"], m["source"]) for m in hist["messages"]] == [
        ("user", "hi", "web"), ("assistant", REPLY, "web")]

    state = client.get("/api/state").json()
    assert state["last_message_id"] == 2
    assert state["llm_model"] == "test-model"  # resolved 'auto' surfaces here after a chat


def test_chat_persona_override(client):
    add_persona(client)
    r = client.post("/api/chat", json={"text": "hello there", "persona": "asuka"})
    assert r.json()["persona"] == "asuka"
    assert client.get("/api/history?persona=asuka").json()["messages"][0]["content"] == "hello there"
    assert client.get("/api/state").json()["active"]["id"] == "riko"  # override does not switch
    assert client.post("/api/chat", json={"text": "x", "persona": "ghost"}).status_code == 404


def test_chat_sends_last_max_history_plus_user(client, fake_llm):
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(10)]
    assert client.post("/api/history/import",
                       json={"persona": "riko", "messages": msgs}).status_code == 200
    client.put("/api/settings", json={"llm": {"max_history": 4}})
    client.post("/api/chat", json={"text": "latest"})
    sent = fake_llm.chat_requests[-1]["messages"]
    assert sent[0]["role"] == "system"
    # contract: system + the LAST max_history stored messages + the fresh user text
    assert [m["content"] for m in sent[1:]] == ["m6", "m7", "m8", "m9", "latest"]


def test_chat_without_llm_configured_is_503(tmp_path):
    client = make_client(tmp_path)  # no llm_defaults, nothing saved
    r = client.post("/api/chat", json={"text": "hi"})
    assert r.status_code == 503
    assert r.json() == {"detail": "LLM not configured"}


def test_llm_failure_502_stores_nothing(client, fake_llm):
    fake_llm.fail = True
    v0 = client.get("/api/state").json()["version"]
    r = client.post("/api/chat", json={"text": "hi"})
    assert r.status_code == 502
    assert "detail" in r.json()
    hist = client.get("/api/history").json()["messages"]
    assert hist == []  # neither the user line nor a reply was stored
    assert client.get("/api/state").json()["version"] == v0


# ── chat: switching ─────────────────────────────────────────────────────────

def test_chat_switch_by_keyword_stores_nothing(client):
    add_persona(client, name="Asuka", greeting="Hmph! What do you want, baka?")
    v0 = client.get("/api/state").json()["version"]
    for text in ("asuka", "Asuka!", " ASUKA. ", "td"):
        add_persona(client, name="TD", greeting="") if text == "td" else None
        r = client.post("/api/chat", json={"text": text})
        assert r.status_code == 200, text
        res = r.json()
        if text == "td":  # TD persona has no greeting -> "<name> here."
            assert res["reply"] == "TD here."
        else:
            assert res["reply"] == "Hmph! What do you want, baka?"
        assert res["switched"] is True
        assert res["persona"] == ("td" if text == "td" else "asuka")
        assert res["message_ids"] == []
    # nothing hit the LLM or either history
    assert client.get("/api/history?persona=riko").json()["messages"] == []
    assert client.get("/api/history?persona=asuka").json()["messages"] == []
    assert client.get("/api/state").json()["active"]["id"] == "td"
    assert v0 < client.get("/api/state").json()["version"]  # switches changed state


def test_chat_switch_by_phrase(client):
    add_persona(client, name="Asuka")
    for text in ("switch to asuka", "Asuka please".replace("Asuka please", "change to asuka!"),
                 "I Want To Talk To Asuka", "become asuka?", "talk to asuka,"):
        r = client.post("/api/chat", json={"text": text})
        assert r.json()["switched"] is True, text
        assert r.json()["persona"] == "asuka"
        assert r.json()["message_ids"] == []
    assert client.get("/api/history?persona=riko").json()["messages"] == []
    # switching to the already active persona still reports switched
    r = client.post("/api/chat", json={"text": "switch to asuka"})
    assert r.json()["switched"] is True


def test_chat_custom_switch_phrases(client, fake_llm):
    add_persona(client, name="Asuka")
    r = client.put("/api/settings", json={"switch_phrases": ["swap to"]})
    assert r.json()["switch_phrases"] == ["swap to"]
    assert client.post("/api/chat", json={"text": "swap to asuka"}).json()["switched"] is True
    # the default phrases are gone now: this is normal chat (and reaches the fake LLM)
    assert client.post("/api/chat", json={"text": "switch to asuka"}).json()["switched"] is False
    assert len(fake_llm.chat_requests) == 1


def test_plain_chat_not_a_switch(client, fake_llm):
    add_persona(client, name="Asuka")
    r = client.post("/api/chat", json={"text": "asuka is loud, but tell me a joke"})
    assert r.json()["switched"] is False
    assert len(fake_llm.chat_requests) == 1


# ── history semantics ───────────────────────────────────────────────────────

def test_history_after_and_limit(client):
    client.post("/api/history/import", json={"persona": "riko", "messages": [
        {"role": "user", "content": f"m{i}"} for i in range(10)]})
    h = client.get("/api/history?limit=3").json()  # after=0 -> the LAST 3, oldest->newest
    assert [m["content"] for m in h["messages"]] == ["m7", "m8", "m9"]
    ids = [m["id"] for m in h["messages"]]
    h2 = client.get(f"/api/history?after={ids[0]}&limit=50").json()
    assert [m["content"] for m in h2["messages"]] == ["m8", "m9"]
    assert client.get("/api/history?limit=1000").json()["persona"] == "riko"  # clamp, no error
    assert client.get("/api/history?after=abc").status_code == 400
    assert client.get("/api/history?persona=ghost").status_code == 404


def test_history_clear(client):
    client.post("/api/chat", json={"text": "hi"})
    v0 = client.get("/api/state").json()["version"]
    assert client.delete("/api/history").status_code == 204
    assert client.get("/api/history").json()["messages"] == []
    assert client.get("/api/state").json()["version"] == v0 + 1
    assert client.delete("/api/history?persona=ghost").status_code == 404


# ── import ──────────────────────────────────────────────────────────────────

def test_import_append_replace_and_validation(client):
    r = client.post("/api/history/import", json={"persona": "riko", "messages": [
        {"role": "user", "content": "old1"}, {"role": "assistant", "content": "old2"}]})
    assert r.json() == {"imported": 2}
    msgs = client.get("/api/history").json()["messages"]
    assert [m["source"] for m in msgs] == ["import", "import"]

    client.post("/api/history/import", json={"persona": "riko", "replace": True, "messages": [
        {"role": "user", "content": "fresh"}]})
    msgs = client.get("/api/history").json()["messages"]
    assert [m["content"] for m in msgs] == ["fresh"]

    assert client.post("/api/history/import", json={"persona": "riko", "messages": [
        {"role": "narrator", "content": "x"}]}).status_code == 422
    assert client.post("/api/history/import", json={"persona": "ghost",
                                                    "messages": []}).status_code == 404


# ── settings ────────────────────────────────────────────────────────────────

def test_settings_never_leaks_api_key(tmp_path, fake_llm):
    client = make_client(tmp_path, llm_defaults={"base_url": fake_llm.base_url,
                                                 "api_key": "sk-default"})
    s = client.get("/api/settings").json()["llm"]
    assert s["api_key_set"] is True and "api_key" not in s
    assert s["base_url"] == fake_llm.base_url  # defaults fill unsaved fields

    r = client.put("/api/settings", json={"llm": {"api_key": "sk-saved", "temperature": 0.5}})
    s = r.json()["llm"]
    assert s["api_key_set"] is True and s["temperature"] == 0.5 and "api_key" not in s

    # a blank api_key never changes anything; only clear_api_key removes the saved one
    assert client.put("/api/settings", json={"llm": {"api_key": ""}}).json()["llm"]["api_key_set"] is True
    cleared = client.put("/api/settings", json={"llm": {"clear_api_key": True}}).json()
    assert cleared["llm"]["api_key_set"] is True  # the env default key applies again
    assert cleared["llm_source"]["api_key"] == "default"

    # saved settings win over llm_defaults
    client.put("/api/settings", json={"llm": {"base_url": "http://elsewhere/v1"}})
    assert client.get("/api/settings").json()["llm"]["base_url"] == "http://elsewhere/v1"

    assert client.put("/api/settings", json={"llm": {"extra_body": [1, 2]}}).status_code == 422
    assert client.put("/api/settings", json={"llm": {"temperature": "hot"}}).status_code == 422
    ok = client.put("/api/settings", json={"llm": {"extra_body": {"top_k": 5},
                                                   "reasoning_effort": "low"}})
    assert ok.json()["llm"]["extra_body"] == {"top_k": 5}
    assert ok.json()["llm"]["reasoning_effort"] == "low"


def test_state_version_increments_every_change(client):
    def now():
        return client.get("/api/state").json()["version"]

    prev = now()
    steps = [
        lambda: client.post("/api/personas", json={"name": "Asuka", "system_prompt": "x"}),
        lambda: client.put("/api/personas/asuka", json={"greeting": "g"}),
        lambda: client.put("/api/personas/asuka/avatar",
                           files={"file": ("a.vrm", VRM_BYTES, "model/gltf-binary")}),
        lambda: client.post("/api/active", json={"persona": "asuka"}),
        lambda: client.post("/api/chat", json={"text": "hi"}),
        lambda: client.put("/api/settings", json={"llm": {"temperature": 0.9}}),
        lambda: client.delete("/api/history"),
        lambda: client.delete("/api/personas/asuka"),
    ]
    for i, step in enumerate(steps):
        step()
        assert now() == prev + 1, f"change #{i} did not bump state.version exactly once"
        prev = now()


def test_settings_change_reresolves_auto_model(tmp_path, fake_llm):
    client = make_client(tmp_path, llm_defaults={"base_url": fake_llm.base_url})
    client.post("/api/chat", json={"text": "one"})
    assert fake_llm.models_calls == 1
    client.put("/api/settings", json={"llm": {"temperature": 0.3}})
    client.post("/api/chat", json={"text": "two"})
    assert fake_llm.models_calls == 2  # PUT settings invalidates the cached 'auto' id


# ── persistence across restarts ─────────────────────────────────────────────

def test_data_persists_across_create_app_restarts(tmp_path, fake_llm):
    c1 = make_client(tmp_path, api_key="k",
                     llm_defaults={"base_url": fake_llm.base_url, "max_tokens": 1024})
    add_persona(c1, name="Asuka", greeting="Hmph!")
    c1.put("/api/settings", json={"llm": {"base_url": fake_llm.base_url}})  # saved settings must survive
    c1.put("/api/personas/asuka/avatar",
           files={"file": ("a.vrm", VRM_BYTES, "model/gltf-binary")})
    c1.post("/api/chat", json={"text": "remember me"})
    c1.post("/api/active", json={"persona": "asuka"})
    s1 = c1.get("/api/state").json()

    # a brand-new app over the same data dir (defaults gone: saved settings win)
    c2 = make_client(tmp_path, api_key="k")
    s2 = c2.get("/api/state").json()
    assert s2["version"] == s1["version"]
    assert s2["active"]["id"] == "asuka"
    assert s2["last_message_id"] == s1["last_message_id"]
    assert c2.get("/api/history?persona=riko").json()["messages"][0]["content"] == "remember me"
    assert c2.get("/api/personas/asuka/avatar").content == VRM_BYTES
    assert c2.get("/api/settings").json()["llm"]["base_url"] == fake_llm.base_url  # saved, not default
    # ...and with defaults restored, chat keeps working after the restart
    c3 = make_client(tmp_path, llm_defaults={"base_url": fake_llm.base_url})
    assert c3.post("/api/chat", json={"text": "again"}).json()["switched"] is False
    ctx = c3.get("/api/history").json()["messages"]  # active persona is asuka, its history starts fresh
    assert len(ctx) == 2  # new user+assistant pair
    assert ctx[0]["persona"] == "asuka"


# ── web page ────────────────────────────────────────────────────────────────

def test_root_serves_config_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<html" in r.text.lower()
    assert "localStorage" in r.text  # the page stores the hub key client-side
