"""Embodiment tests (docs/BODY.md): registration `body`, the body set, the
embodied prompt, action-tag parsing into ChatResult/Message/events, the
switch greeting wave, and the persona appearance/embodied settings.

Tag brackets are built with chr() like the emotion tests so no editor or
filter eats them.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from hub import body as body_mod
from hub import clients as clients_mod
from hub.server import create_app
from hub.store import Store
from test_hub_brain import FakeHubLLM

L = chr(91)  # [
R = chr(93)  # ]


def tag(name):
    return f"{L}{name}{R}"


DESKTOP_BODY = {"actions": ["wave", "think", "nod", "shake_head", "look_around",
                            "stretch", "fidget", "hair_touch", "laugh", "pout",
                            "listen"],
                "expressions": ["happy", "angry", "sad", "relaxed", "surprised"]}

# ── pure parser ─────────────────────────────────────────────────────────────

B = DESKTOP_BODY["actions"]


def test_tag_playable_and_clean_text():
    clean, actions, ignored = body_mod.parse_actions(
        f"Hi there {tag('wave')} nice to see you", B)
    assert clean == "Hi there nice to see you"
    assert [a["name"] for a in actions] == ["wave"]
    assert 0.0 < actions[0]["at"] < 1.0
    assert ignored == []


def test_leading_tag_is_at_zero_and_case_insensitive():
    clean, actions, _ = body_mod.parse_actions(f"{tag('WAVE')} hey", B)
    assert clean == "hey" and actions == [{"name": "wave", "at": 0.0}]
    clean, actions, _ = body_mod.parse_actions(f"{tag('Action: Think')} hmm", B)
    assert clean == "hmm" and actions == [{"name": "think", "at": 0.0}]


def test_at_is_fraction_of_cleaned_text():
    # "Ready. See you." — the wave sat after "Ready. " (7 chars of 15)
    clean, actions, _ = body_mod.parse_actions(f"Ready. {tag('wave')} See you.", B)
    assert clean == "Ready. See you."
    assert actions[0]["at"] == round(7 / 15, 3)


def test_name_normalisation_hyphens_and_spaces():
    _, actions, _ = body_mod.parse_actions(f"{tag('shake-head')} no {tag('look around')}", B)
    assert [a["name"] for a in actions] == ["shake_head", "look_around"]


def test_asterisk_verbs_mapped_and_stripped():
    clean, actions, ignored = body_mod.parse_actions(
        "Okay, *waves* at you, then *thinks* hard, and *shakes head* slowly.", B)
    assert [a["name"] for a in actions] == ["wave", "think", "shake_head"]
    assert "*" not in clean and "waves" not in clean
    assert ignored == []


def test_asterisk_unknown_emotion_stays():
    clean, actions, ignored = body_mod.parse_actions("he *smiles* quietly", B)
    assert clean == "he *smiles* quietly" and actions == [] and ignored == []


def test_known_gesture_missing_from_body_set_is_ignored():
    # `listen` is a known gesture but this client's set is only [wave]
    clean, actions, ignored = body_mod.parse_actions(
        f"{tag('listen')} a moment {tag('wave')}", ["wave"])
    assert clean == "a moment"
    assert [a["name"] for a in actions] == ["wave"]
    assert ignored == ["listen"]


def test_unknown_bracket_tags_untouched():
    # not action requests: mid-text emotion tags, notes, markdown links
    for text in (f"mid {tag('sad')} emotion stays",
                 f"{tag('note to self')} be nice",
                 "[Stardust](https://x) is a game"):
        clean, actions, ignored = body_mod.parse_actions(text, B)
        assert clean == text and actions == [] and ignored == []


def test_explicit_action_prefix_always_stripped():
    clean, actions, ignored = body_mod.parse_actions(f"{tag('action:spin')} look!", B)
    assert clean == "look!" and actions == [] and ignored == ["spin"]


def test_emotion_tag_plus_action_tag_together():
    # the server strips the leading emotion tag first; what reaches the
    # action parser may still carry action tags anywhere
    emotion_free = f"{tag('wave')} Sure thing, senpai. {tag('nod')}"
    clean, actions, _ = body_mod.parse_actions(emotion_free, B)
    assert clean == "Sure thing, senpai."
    assert [a["name"] for a in actions] == ["wave", "nod"]


def test_max_three_and_adjacent_duplicates_merge():
    _, actions, _ = body_mod.parse_actions(
        f"{tag('wave')} a {tag('wave')} b {tag('nod')} c {tag('think')} d {tag('laugh')}", B)
    assert len(actions) == 3                      # 4th dropped
    assert actions[0] == {"name": "wave", "at": 0.0}  # the two waves merged
    assert [a["name"] for a in actions] == ["wave", "nod", "think"]
    _, actions, _ = body_mod.parse_actions(f"{tag('wave')}{tag('wave')} dup", B)
    assert actions == [{"name": "wave", "at": 0.0}]


def test_odd_brackets():
    clean, actions, ignored = body_mod.parse_actions("[[wave]] nested", B)
    assert [a["name"] for a in actions] == ["wave"] and "wave" not in clean
    clean, actions, _ = body_mod.parse_actions("[unclosed wave", B)
    assert clean == "[unclosed wave" and actions == []
    clean, actions, _ = body_mod.parse_actions("[] and [ ] empties", B)
    assert actions == []


def test_no_body_set_gestures_still_stripped_not_played():
    clean, actions, ignored = body_mod.parse_actions(f"{tag('wave')} hi", [])
    assert clean == "hi" and actions == [] and ignored == ["wave"]


def test_body_prompt_names_the_actions():
    p = body_mod.body_prompt(["wave", "think", "nod"])
    assert "Available actions: wave, think, nod" in p
    assert "[wave]" in p and "3D body" in p
    assert len(p.split()) < 130                   # ~120-token budget
    assert body_mod.appearance_prompt("a blue tailcoat") == "Your appearance: a blue tailcoat"


# ── store: persona fields + v3 -> v4 migration ─────────────────────────────

def test_persona_appearance_embodied_roundtrip(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    p = st.add_persona("t", "T", ["t"], "prompt", "g", "v",
                       appearance="blue eyes", embodied=False)
    assert st.get_persona("t")["appearance"] == "blue eyes"
    assert st.get_persona("t")["embodied"] is False
    st.update_persona("t", {"appearance": "", "embodied": True})
    assert st.get_persona("t")["appearance"] == ""
    assert st.get_persona("t")["embodied"] is True
    # appearance is capped at 1000 chars
    st.update_persona("t", {"appearance": "x" * 1500})
    assert len(st.get_persona("t")["appearance"]) == 1000
    st.close()


def test_old_v3_db_migrates_with_body_columns(tmp_path):
    """A pre-embodiment database gains appearance/embodied/actions without
    touching stored rows (docs/BODY.md)."""
    path = tmp_path / "db.sqlite"
    st = Store(path)
    st.add_persona("keep", "K", ["k"], "prompt", "g", "v", appearance="silver hair")
    st.add_message("keep", "assistant", "hi", "app", actions=[{"name": "wave", "at": 0.0}])
    # pretend we are back on schema v3: strip everything v4 added
    st.db.execute("ALTER TABLE personas DROP COLUMN appearance")
    st.db.execute("ALTER TABLE personas DROP COLUMN embodied")
    st.db.execute("ALTER TABLE messages DROP COLUMN actions")
    st.db.execute("PRAGMA user_version=3")
    st.db.commit()
    st.close()

    st2 = Store(path)
    p = st2.get_persona("keep")
    assert p["appearance"] == "" and p["embodied"] is True  # defaults per contract
    assert st2.recent_messages("keep", 10)[0]["actions"] == []
    assert st2.db.execute("PRAGMA user_version").fetchone()[0] == 7  # v5 speech, v6 memory, v7 voice
    st2.close()


# ── registration `body` + body set ─────────────────────────────────────────

@pytest.fixture
def fake_llm():
    s = FakeHubLLM(reply=f"Sure thing, senpai.")
    yield s
    s.stop()


def hub_client(tmp_path, fake_llm):
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": fake_llm.base_url, "model": "auto"})
    return TestClient(app)


def register(client, cid="desk1", body=DESKTOP_BODY, kind="desktop"):
    payload = {"name": cid, "kind": kind, "tools": []}
    if body is not None:
        payload["body"] = body
    return client.post(f"/api/clients/{cid}/register", json=payload)


def test_register_body_validation_and_listing(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    assert register(client).status_code == 200
    # body shows on GET /api/clients; a client without body gets empty lists
    assert register(client, "phone", body=None).status_code == 200
    cl = {c["client_id"]: c for c in client.get("/api/clients").json()["clients"]}
    assert cl["desk1"]["body"]["actions"][0] == "wave"
    assert "talk_wave" not in " ".join(cl["desk1"]["body"]["actions"])
    assert cl["phone"]["body"] == {"actions": [], "expressions": []}
    # validation
    bad = {"name": "x", "kind": "desktop", "tools": [], "body": "yes"}
    assert client.post("/api/clients/d2/register", json=bad).status_code == 422
    bad = {"name": "x", "kind": "desktop", "tools": [], "body": {"actions": "wave"}}
    assert client.post("/api/clients/d2/register", json=bad).status_code == 422
    bad = {"name": "x", "kind": "desktop", "tools": [], "body": {"actions": ["bad name!"]}}
    assert client.post("/api/clients/d2/register", json=bad).status_code == 422


def test_body_set_is_union_over_online_clients(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    register(client, "desk1", body={"actions": ["wave", "nod"]})
    register(client, "desk2", body={"actions": ["nod", "think"]})
    assert sorted(client.get("/api/tools").json()["body_actions"]) == ["nod", "think", "wave"]
    # going offline shrinks the set; the survivor keeps its own actions
    assert client.delete("/api/clients/desk1").status_code == 204
    assert client.get("/api/tools").json()["body_actions"] == ["nod", "think"]
    client.delete("/api/clients/desk2")
    assert client.get("/api/tools").json()["body_actions"] == []


def test_stale_client_leaves_body_set(tmp_path, fake_llm, monkeypatch):
    client = hub_client(tmp_path, fake_llm)
    register(client)
    monkeypatch.setattr(clients_mod, "OFFLINE_AFTER_S", -1.0)  # everyone is stale
    assert client.get("/api/tools").json()["body_actions"] == []


# ── prompt assembly ────────────────────────────────────────────────────────

def body_block_present(fake_llm):
    return "3D body" in fake_llm.chat_requests[-1]["messages"][0]["content"]


def test_prompt_block_only_with_online_embodied_client(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    client.post("/api/chat", json={"text": "hi"})
    assert not body_block_present(fake_llm)          # nobody online
    register(client)
    client.post("/api/chat", json={"text": "hi"})
    assert body_block_present(fake_llm)              # embodied client online
    sys = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert "Available actions: wave" in sys          # names spelled out
    # persona.embodied false: never the block, even with the client online
    client.put("/api/personas/riko", json={"embodied": False})
    client.post("/api/chat", json={"text": "hi"})
    assert not body_block_present(fake_llm)
    client.put("/api/personas/riko", json={"embodied": True})
    # client goes away: block gone again
    client.delete("/api/clients/desk1")
    client.post("/api/chat", json={"text": "hi"})
    assert not body_block_present(fake_llm)


def test_appearance_line_without_any_body(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    client.put("/api/personas/riko",
               json={"appearance": "long white-and-blue hair and a blue tailcoat"})
    client.post("/api/chat", json={"text": "what do you look like?"})
    sys = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert "Your appearance: long white-and-blue hair and a blue tailcoat" in sys
    assert "3D body" not in sys                     # text-only chat
    assert client.get("/api/personas/riko").json()["appearance"] == \
        "long white-and-blue hair and a blue tailcoat"
    too_long = "x" * 1001
    assert client.put("/api/personas/riko",
                      json={"appearance": too_long}).status_code == 422
    r = client.post("/api/personas", json={"name": "Mirror", "appearance": too_long})
    assert r.status_code == 422


# ── chat integration: ChatResult / Message / events ────────────────────────

def test_chat_result_history_and_events_carry_actions(tmp_path):
    llm = FakeHubLLM(reply=f"[happy] Sure thing {tag('wave')} senpai, *nods* agreement.")
    client = hub_client(tmp_path, llm)
    register(client)
    res = client.post("/api/chat", json={"text": "wave at me"}).json()
    assert res["emotion"] == "happy"
    assert [a["name"] for a in res["actions"]] == ["wave", "nod"]
    assert "*" not in res["reply"] and tag("wave") not in res["reply"]
    ai = [m for m in client.get("/api/history").json()["messages"]
          if m["role"] == "assistant"][0]
    assert [a["name"] for a in ai["actions"]] == ["wave", "nod"]
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    fin = [e for e in evs if e["type"] == "turn_finished"][-1]
    assert [a["name"] for a in fin["actions"]] == ["wave", "nod"]
    llm.stop()


def test_chat_actions_empty_without_body_client(tmp_path):
    llm = FakeHubLLM(reply=f"{tag('wave')} hi")   # model waves with no body online
    client = hub_client(tmp_path, llm)
    res = client.post("/api/chat", json={"text": "hi"}).json()
    assert res["actions"] == []
    assert res["reply"] == "hi"                    # stripped + ignored, not spoken
    llm.stop()


def test_ignored_actions_are_logged(tmp_path, fake_llm, caplog):
    llm = FakeHubLLM(reply=f"{tag('action:backflip')} watch this")
    client = hub_client(tmp_path, llm)
    register(client, body={"actions": ["wave"]})
    with caplog.at_level(logging.INFO, logger="riko.hub"):
        res = client.post("/api/chat", json={"text": "show off"}).json()
    assert res["reply"] == "watch this" and res["actions"] == []
    assert any("ignored_actions" in rec.getMessage() and "backflip" in rec.getMessage()
               for rec in caplog.records)
    llm.stop()


def test_switch_greeting_waves_only_with_wave_in_body_set(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    client.post("/api/personas", json={"name": "Asuka", "system_prompt": "x",
                                       "greeting": "Asuka here!"})
    register(client, body={"actions": ["think"]})          # no wave
    res = client.post("/api/chat", json={"text": "asuka"}).json()
    assert res["switched"] and res["actions"] == []
    register(client, body={"actions": ["wave", "think"]})  # re-register with wave
    res = client.post("/api/chat", json={"text": "asuka"}).json()
    assert res["actions"] == [{"name": "wave", "at": 0.0}]
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    fin = [e for e in evs if e["type"] == "turn_finished"][-1]
    assert fin["actions"] == [{"name": "wave", "at": 0.0}]
    # persona not embodied -> no greeting wave even with wave online
    client.put("/api/personas/asuka", json={"embodied": False})
    res = client.post("/api/chat", json={"text": "asuka"}).json()
    assert res["actions"] == []


# ── asked-for requests: matcher, fallback, prompt hint ───────────────────────

# (user text, expected canonical action or None) — conservative matcher
REQUEST_TABLE = [
    # wave requests
    ("wave at me!", "wave"),
    ("Wave at me", "wave"),
    ("can you wave?", "wave"),
    ("could you wave to me", "wave"),
    ("do a wave", "wave"),
    ("give me a wave", "wave"),
    ("hey, wave hello", "wave"),
    ("please wave", "wave"),
    ("wave bye", "wave"),
    ("I miss you. wave at me", "wave"),
    ("hey riko, wave at me!", "wave"),
    # nod
    ("nod", "nod"),
    ("can you nod?", "nod"),
    ("nod for me", "nod"),
    ("Nod yes", "nod"),
    ("please nod your head", "nod"),
    # shake_head
    ("shake your head", "shake_head"),
    ("can you shake your head?", "shake_head"),
    # think: only demo/pose requests
    ("show me your thinking pose", "think"),
    ("show me how you think", "think"),
    ("strike a thinking pose", "think"),
    ("thinking pose please", "think"),
    # stretch / look_around / laugh / pout / hair_touch / fidget
    ("stretch", "stretch"),
    ("can you stretch?", "stretch"),
    ("do a stretch", "stretch"),
    ("look around", "look_around"),
    ("can you look around?", "look_around"),
    ("laugh", "laugh"),
    ("laugh for me", "laugh"),
    ("pout", "pout"),
    ("please pout", "pout"),
    ("touch your hair", "hair_touch"),
    ("can you touch your hair?", "hair_touch"),
    ("fidget", "fidget"),
    ("can you fidget a bit?", "fidget"),
    # mentions, NOT requests
    ("I waved at my friend today", None),
    ("what do you think?", None),
    ("I think so", None),
    ("wave function", None),
    ("don't wave", None),
    ("waves are cool", None),
    ("the crowd waved", None),
    ("she nodded at me", None),
    ("he shook his head", None),
    ("I need to think about it", None),
    ("do you think cats are nice?", None),
    ("I did a stretch this morning", None),
    ("stretch marks hurt", None),
    ("I looked around the room", None),
    ("that made me laugh", None),
    ("you pout too much", None),
    ("her hair is long", None),
    ("stop fidgeting", None),
    ("tell me about wave functions in physics", None),
    ("nobody waved", None),
    ("do you like thinking games?", None),
    ("I touched my hair", None),
    ("laughing at a meme", None),
    ("head shake experiment results", None),
]


@pytest.mark.parametrize("text,want", REQUEST_TABLE,
                         ids=[t[:28] for t, _ in REQUEST_TABLE])
def test_request_matcher_table(text, want):
    assert body_mod.detect_action_request(text) == want


def test_request_matcher_respects_body_set():
    # a request for a gesture the body cannot play is not an action request
    assert body_mod.detect_action_request("wave at me", ["nod"]) is None
    assert body_mod.detect_action_request("wave at me", ["wave"]) == "wave"
    assert body_mod.detect_action_request("wave at me", []) is None
    # name normalisation on the body side
    assert body_mod.detect_action_request("shake your head", ["Shake-Head"]) == "shake_head"


def test_apply_asked_flags_model_tag_and_forces_missing():
    # model tagged it: just flag it, no duplicate, position untouched
    acts = [{"name": "wave", "at": 0.4}]
    out = body_mod.apply_asked("wave at me!", acts, B)
    assert out == [{"name": "wave", "at": 0.4, "asked": True}]
    # model forgot it: added at the start of the reply, flagged
    out = body_mod.apply_asked("wave at me!", [], B)
    assert out == [{"name": "wave", "at": 0.0, "asked": True}]
    # other model actions keep their shape (additive field only)
    out = body_mod.apply_asked("wave at me!", [{"name": "nod", "at": 0.5}], B)
    assert out == [{"name": "wave", "at": 0.0, "asked": True},
                   {"name": "nod", "at": 0.5}]
    # no recognized request: untouched
    acts = [{"name": "nod", "at": 0.5}]
    assert body_mod.apply_asked("what do you think?", acts, B) == acts
    # body set lacks the requested gesture: untouched
    assert body_mod.apply_asked("wave at me!", [], ["nod"]) == []
    # MAX_ACTIONS cap holds
    three = [{"name": "nod", "at": 0.0}, {"name": "think", "at": 0.3},
             {"name": "laugh", "at": 0.6}]
    out = body_mod.apply_asked("wave at me!", list(three), B)
    assert out == three


def test_request_hint_text():
    assert body_mod.request_hint("wave") == \
        "The user just asked you to wave: include [wave] in your reply."


# ── chat integration of the fallback ────────────────────────────────────────

def test_fallback_adds_action_when_llm_replies_in_prose(tmp_path, fake_llm):
    """The whole point: at high temperature the model often ignores the tags
    and 'wave at me!' must still move her (docs/BODY.md §3)."""
    client = hub_client(tmp_path, fake_llm)      # reply: "Sure thing, senpai."
    register(client)
    res = client.post("/api/chat", json={"text": "wave at me!"}).json()
    assert res["actions"] == [{"name": "wave", "at": 0.0, "asked": True}]
    ai = [m for m in client.get("/api/history").json()["messages"]
          if m["role"] == "assistant"][0]
    assert ai["actions"] == [{"name": "wave", "at": 0.0, "asked": True}]
    evs = client.get("/api/events?after=0&wait=0").json()["events"]
    fin = [e for e in evs if e["type"] == "turn_finished"][-1]
    assert fin["actions"] == [{"name": "wave", "at": 0.0, "asked": True}]


def test_fallback_flags_model_tag_without_duplicating(tmp_path):
    llm = FakeHubLLM(reply=f"{tag('wave')} hi there")
    client = hub_client(tmp_path, llm)
    register(client)
    res = client.post("/api/chat", json={"text": "wave at me"}).json()
    assert res["actions"] == [{"name": "wave", "at": 0.0, "asked": True}]
    llm.stop()


def test_fallback_not_applied(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    register(client, body={"actions": ["nod"]})   # body lacks wave
    assert client.post("/api/chat", json={"text": "wave at me"}).json()["actions"] == []
    client.delete("/api/clients/desk1")
    register(client)
    client.put("/api/personas/riko", json={"embodied": False})
    assert client.post("/api/chat", json={"text": "wave at me"}).json()["actions"] == []
    client.put("/api/personas/riko", json={"embodied": True})
    client.delete("/api/clients/desk1")           # no embodied client online
    assert client.post("/api/chat", json={"text": "wave at me"}).json()["actions"] == []
    # a mention is not a request
    register(client)
    res = client.post("/api/chat", json={"text": "I waved at my friend today"}).json()
    assert res["actions"] == []


def test_untouched_turns_have_no_asked_flag(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    register(client)
    res = client.post("/api/chat", json={"text": "what do you think about cats?"}).json()
    assert res["actions"] == []


def test_prompt_hint_only_on_matching_request(tmp_path, fake_llm):
    client = hub_client(tmp_path, fake_llm)
    register(client)
    hint = "The user just asked you to wave"
    client.post("/api/chat", json={"text": "wave at me!"})
    sys = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert hint in sys and "include [wave]" in sys
    client.post("/api/chat", json={"text": "I waved at my friend today"})
    sys = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert hint not in sys
    # no hint when the body cannot play it
    client.delete("/api/clients/desk1")
    register(client, body={"actions": ["nod"]})
    client.post("/api/chat", json={"text": "wave at me!"})
    sys = fake_llm.chat_requests[-1]["messages"][0]["content"]
    assert hint not in sys
