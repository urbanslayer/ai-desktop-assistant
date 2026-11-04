"""Spoken-text tests (hub half): hub/speech.py + how the hub uses it.

The case table tests/data/speech_cases.json is shared with the client mirror
(tests/test_speech_client.py) — both modules must answer it identically. The
integration half runs the real hub against the fake LLM + fake breeze from
test_hub_voice.py: audio is synthesized from the CLEANED text, ChatResult and
history carry `speech`, and the audio cache keys on SPEECH_RULES_VERSION.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import hub.speech as speech_mod
from hub.server import create_app
from hub.speech import LINK_FALLBACK, SPEECH_RULES_VERSION, speech_text
from test_hub_brain import FakeHubLLM
from test_hub_voice import add_persona, fake_breeze, fake_llm, make_client

CASES = json.loads((Path(__file__).parent / "data" / "speech_cases.json").read_text())

# a model reply of the shape docs/TOOLS.md A asks for: spoken line, then a
# blank line, then the links/markdown the owner saw her read out loud
URL_REPLY = ("Found one, it's on your screen!\n\n"
             "![funny cat](https://i.imgur.com/cat.gif)\n"
             "https://i.imgur.com/cat.gif [1]")


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_speech_case_table(case):
    assert speech_text(case["input"]) == case["expect"]


def test_rules_are_idempotent():
    # the client may clean text the hub already cleaned (ChatResult.speech)
    for case in CASES:
        once = speech_text(case["input"])
        if once:
            assert speech_text(once) == once, case["name"]


def test_worded_reply_never_ends_up_empty():
    for case in CASES:
        if any(ch.isalnum() for ch in case["input"]):
            assert case["expect"], case["name"]


# ── hub wiring ────────────────────────────────────────────────────────────────

@pytest.fixture
def url_client(tmp_path, fake_llm, fake_breeze):
    fake_llm.reply = URL_REPLY
    return make_client(tmp_path, llm=fake_llm, tts=fake_breeze)


def test_chat_returns_speech_without_touching_reply(url_client, fake_breeze):
    res = url_client.post("/api/chat", json={"text": "find me a cat gif"}).json()
    # the displayed reply keeps everything
    assert "https://i.imgur.com/cat.gif" in res["reply"]
    assert res["reply"] == URL_REPLY
    # the spoken text is the first paragraph, clean
    assert res["speech"] == "Found one, it's on your screen!"
    assert "http" not in res["speech"] and "!" in res["speech"]
    assert res["audio_url"] == f"/api/messages/{res['message_ids'][1]}/audio"


def test_message_audio_speaks_clean_text_not_urls(url_client, fake_breeze, tmp_path):
    url_client.post("/api/chat", json={"text": "find me a cat gif"})
    r = url_client.get("/api/messages/2/audio")
    assert r.status_code == 200
    said = fake_breeze.speech_requests[-1]["input"]
    assert said == "Found one, it's on your screen!"
    assert "http" not in said and "gif" not in said
    # cache filename carries the speech-rules version
    assert (tmp_path / "hubdata" / "audio"
            / f"2-v1-r{SPEECH_RULES_VERSION}.wav").exists()


def test_audio_cache_invalidated_by_rules_version(url_client, fake_breeze, tmp_path,
                                                 monkeypatch):
    url_client.post("/api/chat", json={"text": "hi"})
    assert url_client.get("/api/messages/2/audio").status_code == 200
    assert len(fake_breeze.speech_requests) == 1
    old = tmp_path / "hubdata" / "audio" / f"2-v1-r{SPEECH_RULES_VERSION}.wav"
    assert old.exists()
    monkeypatch.setattr(speech_mod, "SPEECH_RULES_VERSION", SPEECH_RULES_VERSION + 1)
    assert url_client.get("/api/messages/2/audio").status_code == 200
    assert len(fake_breeze.speech_requests) == 2  # old clip with spoken URLs is dead
    assert (tmp_path / "hubdata" / "audio"
            / f"2-v1-r{SPEECH_RULES_VERSION + 1}.wav").exists()


def test_history_messages_carry_speech(url_client):
    url_client.post("/api/chat", json={"text": "find me a cat gif"})
    msgs = url_client.get("/api/history").json()["messages"]
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    assert assistant["speech"] == "Found one, it's on your screen!"
    assert "http" in assistant["content"]  # display text keeps the links
    user = [m for m in msgs if m["role"] == "user"][-1]
    assert user["speech"] == ""  # user rows never speak


def test_greeting_audio_speaks_clean_text(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    r = client.post("/api/personas", json={
        "name": "Asuka", "system_prompt": "s",
        "greeting": "Asuka here! Details:\n\nhttps://asuka.example/hi"})
    assert r.status_code == 201
    res = client.post("/api/chat", json={"text": "asuka"}).json()
    assert res["switched"] is True
    assert res["speech"] == "Asuka here! Details:"  # cleaned greeting too
    got = client.get(res["audio_url"])
    assert got.status_code == 200
    assert fake_breeze.speech_requests[-1]["input"] == "Asuka here! Details:"
    assert (tmp_path / "hubdata" / "audio"
            / f"greeting-asuka-v1-r{SPEECH_RULES_VERSION}.wav").exists()


def test_tts_route_cleans_before_breeze(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    r = client.post("/api/tts", json={"text": "Look https://x.example/y.gif, cute!\n\nmore"})
    assert r.status_code == 200
    assert fake_breeze.speech_requests[-1]["input"] == "Look, cute!"
    # nothing speakable at all -> 422, never empty audio
    assert client.post("/api/tts", json={"text": "\U0001F602\U0001F602"}).status_code == 422


def test_url_only_reply_falls_back_in_audio(tmp_path, fake_llm, fake_breeze):
    fake_llm.reply = "https://docs.godotengine.org"
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    res = client.post("/api/chat", json={"text": "godot docs link"}).json()
    assert res["reply"] == "https://docs.godotengine.org"
    assert res["speech"] == LINK_FALLBACK
    client.get("/api/messages/2/audio")
    assert fake_breeze.speech_requests[-1]["input"] == LINK_FALLBACK
    assert "http" not in fake_breeze.speech_requests[-1]["input"]


def test_history_backfills_speech_for_old_rows(tmp_path, fake_llm, fake_breeze):
    """Rows imported/stored before the spoken-text round have speech '' —
    GET /api/history fills it so client-side TTS never speaks a URL."""
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    r = client.post("/api/history/import", json={"persona": "riko", "replace": True,
                                                 "messages": [
        {"role": "user", "content": "cat gif?"},
        {"role": "assistant", "content": "Found one!\nhttps://x.example/a.gif"}]})
    assert r.status_code == 200
    msgs = client.get("/api/history").json()["messages"]
    assert msgs[0]["speech"] == ""  # user rows never speak
    assert msgs[1]["speech"] == "Found one!"


def test_prompt_tells_her_not_to_read_urls(tmp_path, fake_llm):
    client = make_client(tmp_path, llm=fake_llm, tts=None)
    client.put("/api/settings", json={"tools": {"enabled": True, "searxng_url":
                                                "http://127.0.0.1:9"}})
    client.post("/api/chat", json={"text": "hi"})
    system = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert "instead of reading URLs aloud" in system
