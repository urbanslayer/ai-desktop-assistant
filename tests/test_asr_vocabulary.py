"""Getting the names right: the Whisper vocabulary prompt and the fix-ups.

Three layers (riko/asr.py + hub): prompt biasing from hub persona names +
`asr.vocabulary:`, exact + fuzzy corrections on the finished text, and the
`voice: true` chat flag. All against fakes — no model, no hub process.
"""
import time
import types

import pytest
import requests
from conftest import HUB_KEY, free_port, make_cfg

from riko import asr, hub_client, session, viewer


def cfg_with(**asr_opts):
    """A remote-asr config with the dotted `asr.<name>` keys, like the yaml."""
    cfg = make_cfg("http://127.0.0.1:1")
    cfg["asr"] = "remote"
    cfg["remote_asr"] = {"url": "http://127.0.0.1:1/v1", "api_key_env": "",
                         "model": "whisper-1", "language": ""}
    for k, v in asr_opts.items():
        cfg[f"asr.{k}"] = v
    return cfg


# ── the prompt: personas + vocabulary, capped ───────────────────────────

def test_prompt_without_hub_uses_defaults():
    cfg = cfg_with()
    prompt = asr.asr_prompt(cfg)
    assert prompt.startswith("Furina,")          # built-in defaults, names first
    assert "senpai" in prompt and prompt.endswith(".")
    assert len(prompt) <= asr.ASR_PROMPT_MAX


def test_prompt_merges_hub_personas_first(hub_connected):
    # fake hub personas: Riko (active), Asuka — active persona leads the list
    names = hub_client.persona_names()
    assert names == ["Riko", "Asuka"]
    prompt = asr.asr_prompt(make_cfg("http://127.0.0.1:1"))
    assert prompt.startswith("Riko, Asuka, Furina")
    assert "Starburst 1200" in prompt
    # persona names are not repeated by the defaults
    assert prompt.count("Riko") == 1


def test_persona_names_cache_and_active_first(hub_connected, fake_hub):
    fake_hub.force_active("asuka")
    hub_client.note_active(hub_client.get().state())
    assert hub_client.persona_names()[0] == "Asuka"  # a switch reorders the list
    # cached: add a persona, the stale list is still served within the TTL
    from conftest import persona
    fake_hub.personas["miku"] = persona("miku", "Miku", "miku", "Hi!")
    assert "Miku" not in hub_client.persona_names()
    hub_client._names_cache = None  # (or 5 minutes of age)
    assert "Miku" in hub_client.persona_names()
    # a dead hub keeps serving the cache instead of raising
    fake_hub.stop()
    assert "Miku" in hub_client.persona_names()


def test_persona_names_without_client():
    assert hub_client.persona_names() == []  # no client: no raise, no names


def test_prompt_adds_user_vocabulary_and_caps():
    cfg = cfg_with(vocabulary=["Naguri", "枫丹"])
    prompt = asr.asr_prompt(cfg)
    assert "Naguri" in prompt and "枫丹" in prompt
    words = ["Word" + str(i) for i in range(120)]  # far past the cap
    cfg = cfg_with(vocabulary=words)
    prompt = asr.asr_prompt(cfg)
    assert len(prompt) <= asr.ASR_PROMPT_MAX
    assert prompt.endswith(".")
    assert "Word0" in prompt          # the front of the list survives...
    assert "Word119" not in prompt    # ...the overflow is dropped whole
    for w in prompt.rstrip(".").split(", "):
        assert w == w.strip() and w    # never cut mid-word


def test_vocabulary_bad_config_is_ignored():
    assert "Furina" in asr.asr_vocabulary(cfg_with(vocabulary=42))
    assert asr.asr_vocabulary({"asr": {"vocabulary": ["Nested"]}})  # nested style too
    assert "Nested" in asr.asr_vocabulary({"asr": {"vocabulary": ["Nested"]}})


# ── the prompt rides with every transcription path ──────────────────────

class FakeCreate:
    """asr._remote / fake groq client: records the create() kwargs."""

    def __init__(self, text="farina says hi"):
        self.calls = []
        self._text = text

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(text=self._text, segments=None)

    @property
    def audio(self):
        return types.SimpleNamespace(transcriptions=self)


def test_remote_transcribe_sends_prompt(monkeypatch):
    fake = FakeCreate()
    monkeypatch.setattr(asr, "_remote", fake)
    cfg = cfg_with()
    assert asr.transcribe(b"RIFF", cfg) == "farina says hi"
    assert fake.calls[0]["prompt"] == asr.asr_prompt(cfg)
    text, evidence = asr.transcribe_verbose(b"RIFF", cfg)
    assert text and evidence is None
    assert fake.calls[1]["prompt"] == asr.asr_prompt(cfg)


def test_groq_transcribe_sends_prompt(monkeypatch):
    fake = FakeCreate()
    monkeypatch.setattr(asr, "OpenAI", lambda **kw: fake)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    cfg = cfg_with()
    cfg["asr"] = "groq"
    asr.transcribe(b"RIFF", cfg)
    assert fake.calls[0]["model"] == asr.GROQ_MODEL
    assert fake.calls[0]["prompt"] == asr.asr_prompt(cfg)


class FakeLocalWhisper:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        seg = types.SimpleNamespace(text=" Farina", no_speech_prob=0.1,
                                    avg_logprob=-0.2, compression_ratio=1.2)
        return [seg], None


def test_local_transcribe_sends_initial_prompt(monkeypatch):
    fake = FakeLocalWhisper()
    monkeypatch.setattr(asr, "_whisper", fake)
    cfg = cfg_with()
    cfg["asr"] = "local"
    assert asr.transcribe(b"RIFF", cfg) == "Farina"
    assert fake.calls[0]["initial_prompt"] == asr.asr_prompt(cfg)
    text, evidence = asr.transcribe_verbose(b"RIFF", cfg)
    assert text == "Farina" and evidence["segments"]
    assert fake.calls[1]["initial_prompt"] == asr.asr_prompt(cfg)


# ── fix-ups: exact table + conservative fuzzy ────────────────────────────

@pytest.mark.parametrize("said,written", [
    ("Farina is here", "Furina is here"),
    ("farina is here", "furina is here"),          # case-insensitive, style kept
    ("FARINA!", "FURINA!"),                        # trailing punctuation stays
    ("Did Fareena wave?", "Did Furina wave?"),
    ("Furena again", "Furina again"),
    ("Fureena again", "Furina again"),
    ("Asooka arrived", "Asuka arrived"),           # fuzzy, Asuka from vocabulary
    ("farina flour", "furina flour"),              # the table is explicit: it wins
])
def test_corrections_fix_mishearings(said, written):
    cfg = cfg_with(vocabulary=["Asuka"])
    assert asr.apply_corrections(said, cfg) == written


def test_correction_identity_entry_switches_one_off():
    cfg = cfg_with(corrections={"Farina": "Farina"})   # "I really mean farina"
    assert asr.apply_corrections("Farina flour", cfg) == "Farina flour"


def test_corrections_add_new_ones():
    cfg = cfg_with(corrections={"Naguri": "Navia"})
    assert asr.apply_corrections("meet Naguri", cfg) == "meet Navia"


@pytest.mark.parametrize("kept", [
    "Marina is nice",        # a real name, not close enough anyway
    "Farm open",             # looks nothing like any vocabulary word
    "My family is here",     # common word, lowercase: untouched
    "Furina is perfect",     # the vocabulary word itself
    "Riko waves",
    "senpai, hi",
    "The Sigma rule",
])
def test_corrections_leave_the_rest(kept):
    cfg = cfg_with()
    assert asr.apply_corrections(kept, cfg) == kept


def test_common_word_guard_blocks_a_close_fuzzy_miss():
    # farina IS a real English word (milled wheat) and fuzzy-close to Furina:
    # the guard stops the fuzzy pass, and an identity correction stops the table
    from difflib import SequenceMatcher
    assert SequenceMatcher(None, "farina", "furina").ratio() >= asr.FUZZY_MIN_RATIO
    cfg = cfg_with(corrections={"Farina": "Farina"})
    assert asr.apply_corrections("farina flour", cfg) == "farina flour"
    assert asr.apply_corrections("Farina flour", cfg) == "Farina flour"


def test_lowercase_words_never_fuzzy_match():
    # only capitalised (name-shaped) words are fuzzy candidates
    assert asr.apply_corrections("furinaa mode", cfg_with()) == "furinaa mode"


def test_corrections_empty_text():
    assert asr.apply_corrections("", cfg_with()) == ""


# ── the session: corrected text is shown and sent, voice turns are flagged ──

@pytest.fixture
def turn_env(monkeypatch, fake_hub, tmp_path):
    port = free_port()
    monkeypatch.setenv("RIKO_PORT", str(port))
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    cfg = make_cfg(fake_hub.url)
    server = viewer.start(cfg)
    hub_client.connect(cfg)
    url = f"http://127.0.0.1:{port}"
    requests.get(f"{url}/next", timeout=5)
    yield cfg, url, fake_hub
    server.shutdown()
    server.server_close()


def wait_say(url, timeout=20):
    events, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        events += requests.get(f"{url}/next", timeout=5).json()["events"]
        if any(e["type"] == "say" for e in events):
            return events
        time.sleep(0.05)
    raise AssertionError(f"no say event: {events}")


def test_voice_turn_is_corrected_flagged_and_shown(turn_env, monkeypatch):
    cfg, url, fake_hub = turn_env
    monkeypatch.setattr(asr, "transcribe", lambda wav, cfg: "Farina, are you there?")
    s = session.Session(cfg, source="desktop")
    s.submit_audio(b"RIFF", source="ptt")
    events = wait_say(url)
    heard = next(e for e in events if e["type"] == "heard")
    assert heard["text"] == "Furina, are you there?"      # bubble shows the fix
    req = fake_hub.chat_requests[-1]
    assert req["text"] == "Furina, are you there?"        # hub gets the fix
    assert req.get("voice") is True                       # and knows it was speech
    history = requests.get(f"{fake_hub.url}/api/history", timeout=5,
                           headers={"Authorization": f"Bearer {HUB_KEY}"}).json()["messages"]
    assert history[0]["voice"] is True                    # 🎤 stored on the user row


def test_typed_turn_has_no_voice_flag(turn_env):
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="desktop")
    s.submit_text("typed hello")
    wait_say(url)
    assert "voice" not in fake_hub.chat_requests[-1]      # old-hub-shaped body


def test_old_hub_ignores_the_voice_field(turn_env):
    """A hub that knows nothing about `voice` (it echoes nothing back and
    stores no flag) still answers: the field is additive."""
    cfg, url, fake_hub = turn_env
    s = session.Session(cfg, source="terminal")
    reply = s.hub.chat("hi", source="terminal", voice=True)
    assert reply["reply"] == "[Riko] you said: hi"      # ChatResult came back normally
    assert "voice" in fake_hub.chat_requests[-1]        # it was sent; hub ignored it
