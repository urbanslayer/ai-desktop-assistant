"""Settings-hardening tests (the blank-on-save incident).

Regression story: the web page once PUT the whole form before its GET had
landed, storing base_url="" (chat -> 503 "LLM not configured"), a
password-manager autofill as the api_key (chat -> 401 from the LLM) and
switch_phrases=[] (phrase switching silently dead). These tests pin the new
semantics: blank = unset -> default, api_key only moves on a non-empty value
or clear_api_key, [] phrases = reset to defaults, plus the reset/test routes.
"""
import pytest
from fastapi.testclient import TestClient

from hub import brain as brain_mod
from hub.server import create_app
from test_hub_brain import FakeHubLLM


@pytest.fixture
def fake_llm():
    server = FakeHubLLM()
    yield server
    server.stop()


def make_client(tmp_path, llm_defaults=None, api_key=None):
    app = create_app(tmp_path / "hubdata", api_key=api_key, llm_defaults=llm_defaults)
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    return TestClient(app, headers=headers)


@pytest.fixture
def env_defaults(tmp_path, fake_llm):
    """A hub started with the RIKO_LLM_* env defaults, like the deployed one."""
    return make_client(tmp_path, llm_defaults={"base_url": fake_llm.base_url,
                                               "api_key": "sk-env-default",
                                               "model": "auto"})


def add_persona(client, name="Asuka"):
    return client.post("/api/personas", json={"name": name, "system_prompt": f"You are {name}.",
                                              "greeting": f"{name} here!"})


# ── the incident: blank save must not break anything ────────────────────────

def test_blank_settings_save_leaves_chat_and_phrases_working(env_defaults, fake_llm):
    """Exactly what the old page sent when it saved before loading."""
    add_persona(env_defaults)
    r = env_defaults.put("/api/settings", json={
        "llm": {"base_url": "", "api_key": "", "temperature": 1.0, "max_tokens": 64000,
                "max_history": 200, "reasoning_effort": "low"},
        "switch_phrases": []})
    assert r.status_code == 200

    chat = env_defaults.post("/api/chat", json={"text": "hi"})
    assert chat.status_code == 200  # not 503, not 401
    assert fake_llm.auth_headers[-1] == "Bearer sk-env-default"

    # phrase switching still works ([] reset to defaults, not "no phrases")
    assert env_defaults.post("/api/chat", json={"text": "switch to asuka"}).json()["switched"] is True
    # the intended edits survived
    llm = env_defaults.get("/api/settings").json()["llm"]
    assert (llm["temperature"], llm["max_tokens"], llm["max_history"],
            llm["reasoning_effort"]) == (1.0, 64000, 200, "low")


def test_blank_base_url_and_model_fall_back_to_defaults(env_defaults):
    env_defaults.put("/api/settings", json={"llm": {"base_url": "http://elsewhere/v1",
                                                    "model": "gpt-ish"}})
    src = env_defaults.get("/api/settings").json()["llm_source"]
    assert (src["base_url"], src["model"]) == ("saved", "saved")
    out = env_defaults.put("/api/settings", json={"llm": {"base_url": "", "model": ""}}).json()
    assert out["llm"]["base_url"] == env_defaults.get("/api/settings").json()["defaults"]["llm"]["base_url"]
    assert out["llm"]["model"] == "auto"
    src = out["llm_source"]
    assert (src["base_url"], src["model"]) == ("default", "default")


def test_legacy_blank_in_database_is_ignored_on_read(tmp_path, fake_llm):
    """Databases written by the old page hold base_url='' — reads must heal them."""
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": fake_llm.base_url, "api_key": "sk-env-default"})
    app.state.store.set_json("llm", {"base_url": "", "api_key": "", "model": ""})
    app.state.store.set_json("switch_phrases", [])
    client = TestClient(app)
    s = client.get("/api/settings").json()
    assert s["llm"]["base_url"] == fake_llm.base_url
    assert s["llm"]["api_key_set"] is True  # the env default shows through again
    assert s["switch_phrases"] == list(brain_mod.DEFAULT_SWITCH_PHRASES)
    assert client.post("/api/chat", json={"text": "hi"}).status_code == 200


# ── api_key rules ────────────────────────────────────────────────────────────

def test_api_key_only_moves_on_typed_value_or_clear(env_defaults, fake_llm):
    env_defaults.put("/api/settings", json={"llm": {"api_key": "sk-typed"}})
    env_defaults.post("/api/chat", json={"text": "one"})
    assert fake_llm.auth_headers[-1] == "Bearer sk-typed"

    # absent / blank api_key leave the saved key alone
    env_defaults.put("/api/settings", json={"llm": {"temperature": 0.5}})
    env_defaults.put("/api/settings", json={"llm": {"api_key": "   "}})
    env_defaults.post("/api/chat", json={"text": "two"})
    assert fake_llm.auth_headers[-1] == "Bearer sk-typed"

    # explicit clear -> the env default is used again
    out = env_defaults.put("/api/settings", json={"llm": {"clear_api_key": True}}).json()
    assert out["llm_source"]["api_key"] == "default"
    env_defaults.post("/api/chat", json={"text": "three"})
    assert fake_llm.auth_headers[-1] == "Bearer sk-env-default"


# ── switch phrases ───────────────────────────────────────────────────────────

def test_empty_switch_phrases_reset_to_defaults(env_defaults):
    env_defaults.put("/api/settings", json={"switch_phrases": ["swap to"]})
    s = env_defaults.put("/api/settings", json={"switch_phrases": []}).json()
    assert s["switch_phrases"] == list(brain_mod.DEFAULT_SWITCH_PHRASES)
    assert s["llm_source"]["switch_phrases"] == "default"


# ── GET shape: defaults + per-field source ───────────────────────────────────

def test_get_exposes_defaults_and_sources(env_defaults):
    s = env_defaults.get("/api/settings").json()
    d = s["defaults"]["llm"]
    assert d["base_url"] == env_defaults.get("/api/settings").json()["llm"]["base_url"]
    assert d["model"] == "auto" and d["api_key_set"] is True and "api_key" not in d
    assert s["defaults"]["switch_phrases"] == list(brain_mod.DEFAULT_SWITCH_PHRASES)
    assert s["llm_source"]["base_url"] == "default"
    env_defaults.put("/api/settings", json={"llm": {"max_tokens": 2048}})
    src = env_defaults.get("/api/settings").json()["llm_source"]
    assert src["max_tokens"] == "saved" and src["base_url"] == "default"


# ── reset route ──────────────────────────────────────────────────────────────

def test_reset_route_drops_saved_overrides(env_defaults, fake_llm):
    env_defaults.put("/api/settings", json={"llm": {"base_url": "http://elsewhere/v1",
                                                    "api_key": "sk-x"},
                                            "switch_phrases": ["swap to"]})
    v0 = env_defaults.get("/api/state").json()["version"]
    out = env_defaults.post("/api/settings/reset",
                            json={"llm": True, "switch_phrases": True}).json()
    assert out["llm"]["base_url"] == fake_llm.base_url  # env default again
    assert all(v == "default" for v in out["llm_source"].values())
    assert out["switch_phrases"] == list(brain_mod.DEFAULT_SWITCH_PHRASES)
    assert env_defaults.get("/api/state").json()["version"] == v0 + 1
    # and chat works against the default again (reset invalidated the auto cache)
    assert env_defaults.post("/api/chat", json={"text": "back"}).status_code == 200


def test_reset_route_validation_and_noop(env_defaults):
    assert env_defaults.post("/api/settings/reset", json={}).status_code == 422
    assert env_defaults.post("/api/settings/reset", json=["llm"]).status_code == 400
    assert env_defaults.post("/api/settings/reset", json="{").status_code == 400
    v0 = env_defaults.get("/api/state").json()["version"]
    assert env_defaults.post("/api/settings/reset", json={"llm": True}).status_code == 200
    assert env_defaults.get("/api/state").json()["version"] == v0  # nothing stored, no bump


# ── test route ───────────────────────────────────────────────────────────────

def test_settings_test_route(env_defaults, fake_llm):
    r = env_defaults.post("/api/settings/test")
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True and out["model"] == "test-model"
    # the voice round adds a tts report (no breeze on this hub -> not ok)
    assert out["tts"] == {"ok": False, "error": "TTS not configured"}


def test_settings_test_route_reports_errors(tmp_path):
    client = make_client(tmp_path)  # no LLM, no TTS at all
    out = client.post("/api/settings/test").json()
    assert out["ok"] is False
    assert out["error"] == "RuntimeError: LLM not configured"
    assert out["tts"] == {"ok": False, "error": "TTS not configured"}
    client.put("/api/settings", json={"llm": {"base_url": "http://127.0.0.1:1/v1"}})
    out = client.post("/api/settings/test").json()
    assert out["ok"] is False and "error" in out and out["error"] != "LLM not configured"


def test_settings_test_never_leaks_key(env_defaults):
    env_defaults.put("/api/settings", json={"llm": {"api_key": "sk-secret-thing"}})
    body = env_defaults.post("/api/settings/test").text
    assert "sk-secret-thing" not in body
    assert "sk-secret-thing" not in env_defaults.get("/api/settings").text
