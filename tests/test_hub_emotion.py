"""Emotion-tag tests: parsing, prompt injection, storage, TTS hint, cache key.

The model is told (when persona.emotes) to open its reply with one tag from
the fixed set; the hub strips it from everything the user sees or hears and
keeps it as structured metadata. Tag brackets are built with chr() like the
thinking tag in test_hub_brain.py so editors/filters can't eat them.
"""
import pytest
from fastapi.testclient import TestClient

from hub import emotion as emo
from hub.server import create_app
from hub.store import Store
from test_hub_brain import FakeHubLLM
from test_hub_voice import FakeBreeze

L = chr(91)  # [
R = chr(93)  # ]


def tag(name):
    return f"{L}{name}{R}"


# ── pure parser ─────────────────────────────────────────────────────────────

def test_parse_recognised_tags_square_and_paren():
    assert emo.parse_emotion(f"{tag('happy')} Hi senpai!") == ("happy", "Hi senpai!")
    assert emo.parse_emotion(f"(teasing) Hello~") == ("teasing", "Hello~")
    assert emo.parse_emotion(f"{tag('thinking')}\n\nlet me see") == ("thinking", "let me see")


def test_parse_missing_and_unknown():
    assert emo.parse_emotion("plain words") == ("neutral", "plain words")
    # unknown tag word: dropped like a tag would be, emotion = neutral
    assert emo.parse_emotion(f"{tag('sleepy')} still works") == ("neutral", "still works")
    # case-insensitive
    assert emo.parse_emotion(f"{tag('Happy')} hey") == ("happy", "hey")
    assert emo.parse_emotion("  " + tag("sad") + "  oops  ") == ("sad", "oops")


def test_parse_tag_only_line():
    assert emo.parse_emotion(tag("embarrassed")) == ("embarrassed", "")


def test_parse_never_eats_markdown_links():
    # a markdown link at the head looks like a tag to a naive parser
    md = "[Stardust](https://en.wikipedia.org/wiki/Stardust) is a 1993 game"
    assert emo.parse_emotion(md) == ("neutral", md)
    # a real tag followed by a markdown link keeps the link intact
    out = emo.parse_emotion(tag("surprised") + " look: [x](http://x)")
    assert out == ("surprised", "look: [x](http://x)")


def test_parse_only_first_tag_removed():
    e, text = emo.parse_emotion(f"{tag('happy')} and then {tag('sad')} again")
    assert e == "happy"
    assert text == f"and then {tag('sad')} again"  # mid-text tags stay (and never speak)


def test_tts_hints_are_emotion_flavoured_but_safe():
    assert emo.tts_hint("neutral") == ""  # no hint = persona's plain style
    assert emo.tts_hint("happy")  # non-empty
    assert emo.tts_hint(None) == ""
    assert emo.tts_hint("not-a-feeling") == ""


# ── store: emotes default + old-db migration ────────────────────────────────

def test_persona_emotes_roundtrip(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    p = st.add_persona("t", "T", ["t"], "prompt", "g", "v", emotes=False)
    assert st.get_persona(p["id"])["emotes"] is False
    st.update_persona(p["id"], {"emotes": True})
    assert st.get_persona(p["id"])["emotes"] is True
    st.close()


def test_old_v2_db_migrates_with_emotes_true(tmp_path):
    """An existing v2 database gains emotes=1, emotion/tools columns and the
    tool_log table without touching stored rows (docs/ROUNDS.md R1-B)."""
    path = tmp_path / "db.sqlite"
    st = Store(path)
    pid = st.add_persona("keep", "K", ["k"], "prompt", "g", "v")["id"]
    st.add_message(pid, "user", "hello", "app")
    # pretend we are back on schema v2: strip everything v3 added
    st.db.execute("DROP TABLE tool_log")
    st.db.execute("ALTER TABLE personas DROP COLUMN emotes")
    st.db.execute("ALTER TABLE messages DROP COLUMN emotion")
    st.db.execute("ALTER TABLE messages DROP COLUMN tools")
    st.db.execute("PRAGMA user_version=2")
    st.db.commit()
    st.close()

    st2 = Store(path)
    assert st2.get_persona(pid)["emotes"] is True  # default on, per contract
    msgs = st2.recent_messages(pid, 10)
    assert msgs[0]["content"] == "hello" and msgs[0]["emotion"] is None
    assert msgs[0]["tools_used"] == []
    assert st2.db.execute("SELECT count(*) FROM tool_log").fetchone()[0] == 0
    st2.close()


# ── chat integration: strip + store + speak ─────────────────────────────────

@pytest.fixture
def fake_llm():
    s = FakeHubLLM(reply=f"{tag('teasing')} Sure thing, senpai.")
    yield s
    s.stop()


@pytest.fixture
def fake_breeze():
    s = FakeBreeze()
    yield s
    s.stop()


def chat_client(tmp_path, fake_llm, fake_breeze=None):
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": fake_llm.base_url, "model": "auto"},
                     tts_defaults={"base_url": fake_breeze.base_url} if fake_breeze else None)
    return TestClient(app)


def test_chat_strips_tag_and_keeps_emotion(tmp_path, fake_llm):
    client = chat_client(tmp_path, fake_llm)
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["reply"] == "Sure thing, senpai."  # tag gone
    assert res["emotion"] == "teasing"
    h = client.get("/api/history").json()
    ai = [m for m in h["messages"] if m["role"] == "assistant"][0]
    assert ai["emotion"] == "teasing"
    assert L not in ai["content"] and R not in ai["content"]


def test_emotes_prompt_line_follows_persona_flag(tmp_path, fake_llm):
    client = chat_client(tmp_path, fake_llm)
    client.post("/api/personas", json={"name": "NoFeel", "system_prompt": "x",
                                       "greeting": "g", "emotes": False})
    client.post("/api/chat", json={"text": "hi"})  # active persona riko (emotes on)
    sysmsg = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert emo.PROMPT_RULE in sysmsg
    client.put("/api/personas/riko", json={"emotes": False})
    fake_llm.chat_requests.clear()
    client.post("/api/chat", json={"text": "hi again"})
    sysmsg = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert emo.PROMPT_RULE not in sysmsg
    # and the persona GET exposes the flag
    assert client.get("/api/personas/riko").json()["emotes"] is False


def test_tts_style_gets_delivery_hint_and_cache_is_per_emotion(tmp_path, fake_llm, fake_breeze):
    client = chat_client(tmp_path, fake_llm, fake_breeze)
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["audio_url"]
    assert client.get(res["audio_url"]).status_code == 200  # GET triggers synthesis
    # the spoken instructions carry the emotion hint, never the raw bracket tag
    req = fake_breeze.speech_requests[-1]
    style = req["instructions"]
    assert emo.tts_hint("teasing") in style
    assert tag("teasing") not in style
    assert req["input"] == "Sure thing, senpai."  # text spoken has no tag either
    # cache file is keyed with the emotion
    aid = res["audio_url"].split("/")[3]
    names = [p.name for p in (tmp_path / "hubdata" / "audio").iterdir()]
    assert any(n == f"{aid}-v1-teasing.wav" for n in names), names
    # a second request re-serves the cache (still one synthesis)
    assert client.get(res["audio_url"]).status_code == 200
    assert len(fake_breeze.speech_requests) == 1
    # same words with a different emotion synthesize again with a different hint
    fake_breeze.speech_requests.clear()
    r2 = client.post("/api/tts", json={"text": "Sure thing, senpai.",
                                       "persona": "riko", "emotion": "sad"})
    assert r2.status_code == 200 and r2.headers["content-type"].startswith("audio/")
    assert emo.tts_hint("sad") in fake_breeze.speech_requests[-1]["instructions"]
    assert client.post("/api/tts", json={"text": "x", "emotion": "nope"}).status_code == 422
