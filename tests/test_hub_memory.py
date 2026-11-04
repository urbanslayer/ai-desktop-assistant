"""Long-term memory round tests (docs/WALK_MEMORY.md M).

Store layer (schema v6 + FTS5, dedup, scopes), the remember/recall/
forget_fact tools through the scripted fake LLM (dedup, scope rules,
forget by id/text, permissions), the budgeted "What you remember" prompt
block (ordering + usage bumps), settings, the /api/memory routes incl.
scoped forget-all and export/import, memory_changed events, and the
assistant-message memory_writes chip data.
"""
import json
import sqlite3

from fastapi.testclient import TestClient

from hub import memory as mem
from hub.events import EventBus
from hub.server import MEMORY_DEFAULTS, create_app
from hub.store import SCHEMA_VERSION, Store
from hub.tools.memory_tools import build_memory_specs
from test_hub_tools import ScriptedLLM, call_step


def llm_client(tmp_path, steps, tools_defaults=None, memory_defaults=None):
    llm = ScriptedLLM(steps)
    td = {"searxng_url": ""}
    td.update(tools_defaults or {})
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     tools_defaults=td, memory_defaults=memory_defaults or {})
    return TestClient(app), llm


def facts_of(client, **params):
    r = client.get("/api/memory", params=params)
    assert r.status_code == 200
    return r.json()["facts"]


def memory_tool(client, name, writes=None):
    specs = {s.name: s for s in build_memory_specs(
        client.app.state.store, EventBus(), "riko", writes if writes is not None else [])}
    return specs[name]


# ── store: migration, FTS5, fallback ────────────────────────────────────────

def test_fts5_is_available_and_new_db_is_v6(tmp_path):
    # The contract pins this: python:3.12-slim's sqlite3 must have FTS5, and
    # the tests run on the same interpreter the hub ships with.
    s = Store(tmp_path / "hub.db")
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
    assert s.fts_enabled is True
    tables = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master")}
    assert {"facts", "facts_fts"} <= tables
    cols = {r["name"] for r in s.db.execute("PRAGMA table_info(messages)")}
    assert "memory_writes" in cols
    s.close()


def test_old_v5_database_migrates(tmp_path):
    path = tmp_path / "hub.db"
    db = sqlite3.connect(path)
    # hand-build the pre-memory schema (what v1..v5 migrations leave behind)
    db.executescript("""
      CREATE TABLE personas(id TEXT PRIMARY KEY, name TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '[]', system_prompt TEXT NOT NULL DEFAULT '',
        greeting TEXT NOT NULL DEFAULT '', tts_voice TEXT NOT NULL DEFAULT '',
        avatar_version INTEGER NOT NULL DEFAULT 0, has_avatar INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        voice TEXT NOT NULL DEFAULT '', voice_version INTEGER NOT NULL DEFAULT 1,
        emotes INTEGER NOT NULL DEFAULT 1, appearance TEXT NOT NULL DEFAULT '',
        embodied INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, persona TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, emotion TEXT, tools TEXT NOT NULL DEFAULT '',
        actions TEXT NOT NULL DEFAULT '', speech TEXT NOT NULL DEFAULT '');
      CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE tool_log(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        persona TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL DEFAULT '',
        ok INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '',
        ms INTEGER NOT NULL DEFAULT 0, approval TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'hub');
      INSERT INTO personas(id,name,created_at,updated_at,voice)
        VALUES('riko','Riko','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','{}');
      INSERT INTO messages(persona,role,content,source,created_at)
        VALUES('riko','user','old question','web','2026-01-01T00:00:00Z');
      INSERT INTO meta VALUES('version','7');
    """)
    db.execute("PRAGMA user_version=5")
    db.commit()
    db.close()

    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert s.fts_enabled and s.get_persona("riko")["name"] == "Riko"
    assert s.recent_messages("riko", 10)[0]["content"] == "old question"
    assert s.recent_messages("riko", 10)[0]["memory_writes"] == []  # legacy row
    assert s.version() == 7                                          # meta intact
    f = s.add_fact("cat named Miso")
    assert s.search_facts("cat", scopes=["user"])[0]["id"] == f["id"]
    s.close()


def test_fts_index_backfills_when_it_appears_later(tmp_path):
    path = tmp_path / "hub.db"
    s = Store(path)
    s.add_fact("the garage code is 1234")
    # simulate a DB written by a SQLite without FTS5: index + triggers gone
    s.db.executescript("DROP TABLE facts_fts; DROP TRIGGER facts_ai; "
                       "DROP TRIGGER facts_ad; DROP TRIGGER facts_au;")
    s.db.commit()
    s.close()
    s2 = Store(path)
    assert s2.fts_enabled
    hits = s2.search_facts("garage", scopes=["user"])
    assert [h["text"] for h in hits] == ["the garage code is 1234"]
    s2.close()


def test_like_fallback_without_fts(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.add_fact("birthday is March 3rd")
    s.fts_enabled = False
    assert s.search_facts("birthday", scopes=["user"])[0]["text"] == "birthday is March 3rd"
    assert s.search_facts("nothing here", scopes=["user"]) == []
    s.close()


# ── store: dedup, scopes, usage ─────────────────────────────────────────────

def test_upsert_dedup_and_update(tmp_path):
    s = Store(tmp_path / "hub.db")
    f1, upd1 = s.upsert_fact("My cat is called Miso", scope="user")
    f2, upd2 = s.upsert_fact("  my cat is CALLED miso. ", scope="user")
    assert not upd1 and upd2 and f1["id"] == f2["id"]
    assert f2["text"] == "my cat is CALLED miso."       # text refreshed
    f3, upd3 = s.upsert_fact("My cat is called Miso", scope="persona:riko")
    assert not upd3 and f3["id"] != f1["id"]             # scope boundary respected
    f4, upd4 = s.upsert_fact("I work on Starburst 1200", scope="user")
    assert f4["id"] != f1["id"]                          # different facts survive
    # a dedup update keeps an existing pin
    s.db.execute("UPDATE facts SET pinned=1 WHERE id=?", (f1["id"],))
    s.db.commit()
    f5, upd5 = s.upsert_fact("my cat is called miso", scope="user")
    assert upd5 and f5["pinned"]
    # cross-scope dedup (reviewer fix): a persona note that repeats a user
    # fact (same wording — the model's paraphrases vary too much for fuzzy
    # matching, but the identical case is common) merges into the user fact
    f6, upd6 = s.upsert_fact("My cat is called Miso", scope="persona:riko",
                             dedup_scopes=["user"])
    assert upd6 and f6["id"] == f1["id"] and f6["scope"] == "user"
    s.close()


def test_touch_and_persona_scoped_delete(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.add_persona(pid="mia", name="Mia", keywords=[], system_prompt="", greeting="",
                  tts_voice="", voice={})
    f = s.add_fact("private note", scope="persona:mia")
    s.touch_facts([f["id"]])
    g = s.get_fact(f["id"])
    assert g["use_count"] == 1 and g["last_used_at"]
    gone = s.delete_persona("mia")
    assert gone == [f["id"]] and s.get_fact(f["id"]) is None
    # a user fact survives persona death
    s.add_fact("shared stays", scope="user")
    s.delete_persona("riko")
    page, total = s.list_facts(scopes=["user"])
    assert total == 1 and [x["text"] for x in page] == ["shared stays"]
    s.close()


# ── tools through the fake LLM ──────────────────────────────────────────────

def test_remember_stores_fact_and_chips_the_reply(tmp_path):
    client, llm = llm_client(tmp_path, [
        call_step("remember", {"text": "My cat is called Miso"}),
        {"content": "Noted, Miso! 🐱"},
    ])
    r = client.post("/api/chat", json={"text": "my cat is called Miso", "source": "web"})
    assert r.status_code == 200
    res = r.json()
    assert res["memory_writes"] == [{"id": 1, "text": "My cat is called Miso"}]
    facts = facts_of(client)
    assert [f["text"] for f in facts] == ["My cat is called Miso"]
    assert facts[0]["scope"] == "user"
    # the fact rides on the assistant message (the 🧠 chip data) and history
    h = client.get("/api/history", params={"persona": "riko", "limit": 10}).json()
    assert h["messages"][-1]["memory_writes"] == [{"id": 1, "text": "My cat is called Miso"}]
    assert {"tool": "remember", "label": "remembering: My cat is called Miso"} \
        in res["tools_used"]
    offered = [t["function"]["name"] for t in llm.chat_requests[0].get("tools", [])]
    assert {"remember", "recall", "forget_fact"} <= set(offered)


def test_remember_dedups_across_turns(tmp_path):
    client, _ = llm_client(tmp_path, [
        call_step("remember", {"text": "I work on a game called Starburst 1200"}),
        {"content": "ok"},
    ])
    client.post("/api/chat", json={"text": "first", "source": "web"})
    client.post("/api/chat", json={"text": "again", "source": "web"})
    assert len(facts_of(client)) == 1                    # second remember updated it


def test_persona_notes_are_private_until_the_persona_dies(tmp_path):
    client, llm = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/memory", json={"text": "I work on Starburst 1200"})
    other = client.post("/api/personas", json={
        "name": "Mia", "system_prompt": "You are Mia.", "greeting": "hi"}).json()
    client.post("/api/memory", json={"text": "riko-only note about a koi pond",
                                     "scope": "persona:riko"})
    r = client.post("/api/chat", json={"text": "what game am I making?", "source": "web",
                                       "persona": other["id"]})
    assert r.status_code == 200 and r.json()["persona"] == other["id"]
    sys_msg = llm.chat_requests[-1]["messages"][0]["content"]
    assert "Starburst 1200" in sys_msg       # user fact: shared across personas
    assert "koi pond" not in sys_msg         # riko's private note stays private
    # switch back: the note is visible again
    client.post("/api/chat", json={"text": "about the fish?", "source": "web"})
    assert "koi pond" in llm.chat_requests[-1]["messages"][0]["content"]
    # deleting riko takes her notes, keeps the user facts
    d = client.delete("/api/personas/riko")
    assert d.status_code == 204
    left = facts_of(client)
    assert [f["text"] for f in left] == ["I work on Starburst 1200"]


def test_forget_by_text_and_id(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/memory", json={"text": "My cat is called Miso"})
    fid = facts_of(client)[0]["id"]
    out = json.loads(memory_tool(client, "forget_fact").run({"text": "cat Miso"}))
    assert out == {"deleted": fid, "text": "My cat is called Miso"}
    assert facts_of(client) == []
    client.post("/api/memory", json={"text": "parking spot B-12"})
    fid = facts_of(client)[0]["id"]
    out = json.loads(memory_tool(client, "forget_fact").run({"id": fid}))
    assert out["deleted"] == fid
    assert facts_of(client) == []
    import pytest
    with pytest.raises(RuntimeError, match="needs an id or a text"):
        memory_tool(client, "forget_fact").run({})


def test_forget_ambiguous_text_returns_candidates(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/memory", json={"text": "favorite color is red"})
    client.post("/api/memory", json={"text": "favorite shirt color is red"})
    out = json.loads(memory_tool(client, "forget_fact").run({"text": "color red"}))
    assert out.get("error") and len(out["candidates"]) >= 2
    assert len(facts_of(client)) == 2                     # nothing deleted
    out2 = json.loads(memory_tool(client, "forget_fact").run(
        {"id": out["candidates"][0]["id"]}))
    assert out2["deleted"]
    assert len(facts_of(client)) == 1


def test_recall_scopes_and_empty(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/memory", json={"text": "cat named Miso"})
    writes: list = []
    out = json.loads(memory_tool(client, "recall", writes).run({"query": "cat"}))
    assert out["count"] == 1 and out["facts"][0]["text"] == "cat named Miso"
    assert out["facts"][0]["scope"] == "user"
    assert json.loads(memory_tool(client, "recall", writes).run(
        {"query": "zzz nothing"}))["count"] == 0
    # recall usage bumps
    assert facts_of(client)[0]["use_count"] >= 1


def test_permission_never_hides_and_ask_auto_runs(tmp_path):
    client, llm = llm_client(tmp_path, [{"content": "ok"}])
    r = client.put("/api/settings", json={"tools": {"permissions": {
        "remember": "never", "recall": "never", "forget_fact": "never"}}})
    assert r.status_code == 200
    client.post("/api/chat", json={"text": "hi", "source": "web"})
    offered = [t["function"]["name"] for t in llm.chat_requests[-1].get("tools", [])]
    assert "remember" not in offered
    # never also survives a direct call (the loop would refuse it)
    # ask + nobody online: the hub gate auto-approves and the tool runs
    client.put("/api/settings", json={"tools": {"permissions": {"remember": "ask"}}})
    client2, _ = llm_client(tmp_path, [
        call_step("remember", {"text": "likes dark roast coffee"}),
        {"content": "ok"},
    ])
    r = client2.post("/api/chat", json={"text": "note this", "source": "web"})
    assert r.status_code == 200
    assert any(f["text"] == "likes dark roast coffee" for f in facts_of(client2))
    log = client2.get("/api/tools/log", params={"limit": 5}).json()["entries"]
    assert log[0]["tool"] == "remember" and log[0]["approval"].startswith("auto")


def test_memory_off_removes_block_and_tools(tmp_path):
    client, llm = llm_client(tmp_path, [{"content": "ok"}],
                             memory_defaults={"enabled": False})
    client.post("/api/memory", json={"text": "anything"})
    client.post("/api/chat", json={"text": "hi", "source": "web"})
    req = llm.chat_requests[-1]
    offered = [t["function"]["name"] for t in req.get("tools", [])]
    assert "remember" not in offered
    assert "What you remember" not in req["messages"][0]["content"]


# ── the prompt block ────────────────────────────────────────────────────────

def test_prompt_block_ordering_budget_and_usage(tmp_path):
    client, llm = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/memory", json={"text": "PINNED: allergic to peanuts", "pinned": True})
    for i in range(40):
        client.post("/api/memory", json={"text": f"filler fact number {i} " + "x" * 90})
    client.post("/api/memory", json={"text": "the cat Miso sleeps on the keyboard"})
    client.post("/api/chat", json={"text": "where does the cat sleep?", "source": "web"})
    sys_msg = llm.chat_requests[-1]["messages"][0]["content"]
    assert "What you remember" in sys_msg
    assert "PINNED: allergic to peanuts" in sys_msg
    lines = [l for l in sys_msg.splitlines() if l.startswith("- ")]
    assert lines[0].startswith("- PINNED")             # pinned first
    assert any("keyboard" in l for l in lines[1:4])    # then the FTS match
    assert len(lines) < 42                             # budget clipped the fillers
    facts = {f["text"]: f for f in facts_of(client, limit=100)}
    assert facts["PINNED: allergic to peanuts"]["use_count"] == 1
    assert facts["the cat Miso sleeps on the keyboard"]["use_count"] == 1
    # a tiny budget keeps pinned but shrinks the block
    client.put("/api/settings", json={"memory": {"prompt_budget_tokens": 120}})
    client.post("/api/chat", json={"text": "again", "source": "web"})
    sys2 = llm.chat_requests[-1]["messages"][0]["content"]
    assert "PINNED: allergic to peanuts" in sys2
    assert len([l for l in sys2.splitlines() if l.startswith("- ")]) < len(lines)


def test_prompt_block_absent_without_facts(tmp_path):
    client, llm = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/chat", json={"text": "hi", "source": "web"})
    assert "What you remember" not in llm.chat_requests[-1]["messages"][0]["content"]


# ── settings + routes + events ──────────────────────────────────────────────

def test_settings_memory_defaults_save_reset(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    s = client.get("/api/settings").json()
    assert s["memory"] == dict(MEMORY_DEFAULTS)
    assert s["defaults"]["memory"] == dict(MEMORY_DEFAULTS)
    assert set(s["memory_source"]) == set(MEMORY_DEFAULTS)
    assert all(v == "default" for v in s["memory_source"].values())
    r = client.put("/api/settings", json={"memory": {"enabled": False,
                                                     "auto_remember": False,
                                                     "prompt_budget_tokens": 250}})
    assert r.json()["memory"] == {"enabled": False, "prompt_budget_tokens": 250,
                                  "auto_remember": False}
    assert set(client.get("/api/settings").json()["memory_source"].values()) == {"saved"}
    assert client.put("/api/settings",
                      json={"memory": {"prompt_budget_tokens": 50}}).status_code == 422
    assert client.put("/api/settings",
                      json={"memory": {"enabled": "yes"}}).status_code == 422
    r = client.post("/api/settings/reset", json={"memory": True})
    assert r.json()["memory"] == dict(MEMORY_DEFAULTS)


def test_routes_crud_scopes_and_confirm(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    other = client.post("/api/personas", json={
        "name": "Mia", "system_prompt": "x", "greeting": "hi"}).json()
    f = client.post("/api/memory", json={"text": "cat Miso", "scope": "user"})
    assert f.status_code == 201
    fid = f.json()["id"]
    assert client.post("/api/memory", json={"text": "  "}).status_code == 422
    assert client.post("/api/memory",
                       json={"text": "x", "scope": "persona:nope"}).status_code == 422
    assert client.post("/api/memory",
                       json={"text": "x", "scope": f"persona:{other['id']}"}).status_code == 201
    dup = client.post("/api/memory", json={"text": "cat miso"}).json()
    assert dup["id"] == fid and dup["updated"] is True          # dedup via the API
    assert client.put(f"/api/memory/{fid}", json={"pinned": True}).json()["pinned"] is True
    assert client.put(f"/api/memory/{fid}",
                      json={"text": "cat Miso loves tuna"}).json()["text"] == "cat Miso loves tuna"
    assert client.put("/api/memory/9999", json={"pinned": True}).status_code == 404
    assert client.put(f"/api/memory/{fid}", json={}).status_code == 422
    assert client.put(f"/api/memory/{fid}",
                      json={"text": " " * 600}).status_code == 422
    assert facts_of(client, scope="user") and facts_of(client, scope="user")[0]["pinned"]
    hits = client.get("/api/memory", params={"q": "tuna"}).json()["facts"]
    assert [h["id"] for h in hits] == [fid]
    assert client.get("/api/memory", params={"scope": "bogus"}).status_code == 422
    # forget-everything needs the typed confirmation
    assert client.delete("/api/memory", params={"scope": "all"}).status_code == 400
    assert client.delete("/api/memory", params={"scope": "persona:nope",
                                                "confirm": "yes"}).status_code == 404
    only_mia = client.delete("/api/memory", params={"scope": f"persona:{other['id']}",
                                                    "confirm": "yes"})
    assert only_mia.json() == {"deleted": 1}
    assert all(f["scope"] != f"persona:{other['id']}" for f in facts_of(client))
    assert client.delete(f"/api/memory/{fid}").status_code == 204
    assert client.delete(f"/api/memory/{fid}").status_code == 404
    assert client.delete("/api/memory",
                         params={"scope": "all", "confirm": "yes"}).json() == {"deleted": 0}


def test_export_import_round_trip(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    client.post("/api/memory", json={"text": "cat Miso"})
    client.post("/api/memory", json={"text": "keep this pinned", "pinned": True})
    exp = client.get("/api/memory/export").json()
    assert exp["version"] == 1 and len(exp["facts"]) == 2

    # import into a fresh hub over a different data dir (persona riko is seeded)
    llm = ScriptedLLM([{"content": "ok"}])
    app2 = create_app(tmp_path / "other-data", llm_defaults={"base_url": llm.base_url})
    client_b = TestClient(app2)
    r = client_b.post("/api/memory/import", json=exp)
    assert r.status_code == 200 and r.json()["imported"] == 2
    back = client_b.get("/api/memory").json()["facts"]
    assert {f["text"] for f in back} == {"cat Miso", "keep this pinned"}
    pinned = {f["text"]: f["pinned"] for f in back}
    assert pinned["keep this pinned"] is True and pinned["cat Miso"] is False
    assert client_b.post("/api/memory/import",
                         json={"facts": [{"text": ""}]}).status_code == 422
    assert client_b.post("/api/memory/import",
                         json={"facts": [{"text": "x", "scope": "persona:ghost"}]}
                         ).status_code == 422
    r = client_b.post("/api/memory/import",
                      json={"facts": [{"text": "only this"}], "replace": True})
    assert r.json() == {"imported": 1, "replaced": 2}
    assert [f["text"] for f in client_b.get("/api/memory").json()["facts"]] == ["only this"]
    llm.stop()


def test_memory_changed_events(tmp_path):
    client, _ = llm_client(tmp_path, [{"content": "ok"}])
    v0 = client.get("/api/events", params={"after": 0, "wait": 0}).json()["last"]
    f = client.post("/api/memory", json={"text": "event me"}).json()
    client.put(f"/api/memory/{f['id']}", json={"pinned": True})
    client.delete(f"/api/memory/{f['id']}")
    evs = client.get("/api/events", params={"after": v0, "wait": 0}).json()["events"]
    kinds = {(e["type"], e.get("op"), e.get("id")) for e in evs
             if e["type"] == "memory_changed"}
    assert ("memory_changed", "added", f["id"]) in kinds
    assert ("memory_changed", "updated", f["id"]) in kinds
    assert ("memory_changed", "deleted", f["id"]) in kinds
    assert client.get("/api/state").json()["version"] > v0


# ── memory.py unit bits ─────────────────────────────────────────────────────

def test_helpers():
    assert mem.clamp_fact_text("  a\n b  ") == "a b"
    assert mem.clamp_fact_text("x" * 900) == "x" * mem.MAX_FACT_CHARS
    assert mem.normalize_text("  My Café Cat!! ") == "my cafe cat"
    assert mem.similar(mem.normalize_text("cat named miso"),
                       mem.normalize_text("Cat named Miso.")) == 1.0
    assert mem.similar("cat named miso", "i love koi ponds") < 0.5
    assert mem.fts_match('what\'s my cat\'s "name"?') == \
        '"what\'\'s" OR "my" OR "cat\'\'s" OR "name"'
    assert mem.fts_match("") == ""
    assert mem.fts_tokens("aaa b") == ["aaa", "b"]
    block, picked = mem.build_memory_block(
        [{"id": 1, "text": "tiny", "pinned": True}], True, True, 600)
    assert "What you remember" in block and len(picked) == 1
    assert "forget_fact" in block
    block, picked = mem.build_memory_block(
        [{"id": 1, "text": "t" * 4000, "pinned": False}], True, False, 100)
    assert block == "" and picked == []
