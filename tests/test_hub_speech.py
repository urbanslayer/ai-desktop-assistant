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
from hub.speech import LINK_FALLBACK, MAX_SPOKEN_CHARS, SPEECH_RULES_VERSION, speech_text
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
    assert res["speech"] == "Asuka here! Details."  # cleaned greeting too
    got = client.get(res["audio_url"])
    assert got.status_code == 200
    assert fake_breeze.speech_requests[-1]["input"] == "Asuka here! Details."
    assert (tmp_path / "hubdata" / "audio"
            / f"greeting-asuka-v1-r{SPEECH_RULES_VERSION}.wav").exists()


def test_tts_route_cleans_before_breeze(tmp_path, fake_llm, fake_breeze):
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    r = client.post("/api/tts", json={"text": "Look https://x.example/y.gif, cute!\n\nhttps://x.example/y.gif"})
    assert r.status_code == 200
    assert fake_breeze.speech_requests[-1]["input"] == "Look that website, cute!"
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
    assert "never write a URL or website address" in system
    assert "name the site instead" in system
    # the multi-paragraph rule: only the links/sources tail is shown-not-spoken
    assert "put links and sources at the end, after a blank line" in system
    assert "spoken in full" in system


# ── the whole-reply fix: prose past paragraph one must be spoken ─────────────

LONG_REPLY = (
    "Sure — three things helped me there.\n\n"
    "First, keep the reply in prose: the bubble keeps every paragraph.\n\n"
    "Second, links belong at the end, after a blank line.\n\n"
    "Sources:\n- [docs](https://docs.godotengine.org/x)\n- https://blog.example/post\n\n"
    "Third, this last paragraph is spoken too, even after the links block."
)

SPOKEN_LONG = ("Sure — three things helped me there. "
               "First, keep the reply in prose: the bubble keeps every paragraph. "
               "Second, links belong at the end, after a blank line. "
               "Third, this last paragraph is spoken too, even after the links block.")


def test_whole_multi_paragraph_reply_is_spoken(tmp_path, fake_llm, fake_breeze):
    """Owner bug: a long reply spoke only sentence or two — speech_text used
    to cut at the first blank line. Every prose paragraph must reach breeze;
    only the Sources block is dropped."""
    fake_llm.reply = LONG_REPLY
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    res = client.post("/api/chat", json={"text": "explain it properly"}).json()
    assert res["reply"] == LONG_REPLY          # display keeps everything
    assert res["speech"] == SPOKEN_LONG
    assert "http" not in res["speech"] and "Sources" not in res["speech"]
    got = client.get(res["audio_url"])
    assert got.status_code == 200
    # ONE request: Breeze splits long text on sentence boundaries itself
    # (hub/voice.py header, docs/server.md v0.1.6) — the hub must not chunk.
    assert len(fake_breeze.speech_requests) == 1
    assert fake_breeze.speech_requests[-1]["input"] == SPOKEN_LONG


def test_spoken_text_is_capped_at_a_sentence_end(tmp_path, fake_llm, fake_breeze):
    unit = "That is a perfectly fine sentence, senpai."
    fake_llm.reply = "\n\n".join([" ".join([unit] * 12)] * 6)  # ~3000 chars
    client = make_client(tmp_path, llm=fake_llm, tts=fake_breeze)
    res = client.post("/api/chat", json={"text": "go on"}).json()
    spoken = res["speech"]
    assert 0 < len(spoken) <= MAX_SPOKEN_CHARS
    assert spoken.endswith(unit)               # cut exactly at a sentence end
    client.get(res["audio_url"])
    assert fake_breeze.speech_requests[-1]["input"] == spoken
