"""SQLite store for the hub: personas, messages, settings and the state version.

One connection, ``check_same_thread=False``, guarded by a lock — FastAPI runs
sync endpoints on its threadpool, so every access goes through the lock.
WAL mode keeps readers from blocking the writer. Schema migrations use the
``user_version`` pragma (SCHEMA_VERSION below is the target).
"""
import json
import random
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

try:  # package import (repo root) vs standalone (`python server.py` in hub/)
    from . import memory as _memory
except (ImportError, SystemError):  # standalone: hub/ is on sys.path
    import memory as _memory  # type: ignore[no-redef]

SCHEMA_VERSION = 6

# Voice migration for personas written before the voice round (docs/VOICE.md):
# a bland design-mode voice with a random seed, so they keep talking after the
# upgrade until the user personalises them.
GENERIC_VOICE_DESCRIPTION = "A young woman with a clear, friendly voice."


def default_voice(description: str = GENERIC_VOICE_DESCRIPTION) -> dict:
    """Fresh per-persona voice: design mode, fixed random seed for stability."""
    return {
        "mode": "design",
        "description": description,
        "style": "",
        "ref_text": "",
        "has_reference": False,
        "seed": random.randint(1, 2_147_483_646),
        "cfg_scale": None,
    }

_SCHEMA_V1 = """
CREATE TABLE personas(
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  keywords       TEXT NOT NULL DEFAULT '[]',   -- JSON array
  system_prompt  TEXT NOT NULL DEFAULT '',
  greeting       TEXT NOT NULL DEFAULT '',
  tts_voice      TEXT NOT NULL DEFAULT '',
  avatar_version INTEGER NOT NULL DEFAULT 0,
  has_avatar     INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE messages(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,   -- global, monotonic
  persona    TEXT NOT NULL,
  role       TEXT NOT NULL,                      -- user | assistant
  content    TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT '',           -- desktop | terminal | android | web | import
  created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_persona ON messages(persona, id);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- JSON values
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);      -- version, active
"""

# Emotion + tools round (docs/ROUNDS.md R1-B). The tool_log is a ring in
# practice: keep_tool_log() trims to TOOL_LOG_KEEP newest rows.
_TOOL_LOG_KEEP = 1000
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS tool_log(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  persona    TEXT NOT NULL,
  tool       TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'hub',    -- hub | client
  arguments  TEXT NOT NULL DEFAULT '',       -- JSON, truncated
  ok         INTEGER NOT NULL DEFAULT 1,
  error      TEXT NOT NULL DEFAULT '',
  ms         INTEGER NOT NULL DEFAULT 0,
  approval   TEXT NOT NULL DEFAULT ''        -- '' | approved | denied | not approved | …
);
"""

# Long-term memory round (docs/WALK_MEMORY.md M): a facts table plus an FTS5
# index. The FTS index is a plain content table kept in sync by triggers —
# if this Python's SQLite has no FTS5 module the hub degrades to LIKE search
# (Store.fts_enabled is then False) and nothing else changes.
_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS facts(
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  text              TEXT NOT NULL,
  scope             TEXT NOT NULL DEFAULT 'user',   -- user | persona:<id>
  pinned            INTEGER NOT NULL DEFAULT 0,
  source_message_id INTEGER,                        -- the user message it came from
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  last_used_at      TEXT,                           -- last time it entered a prompt
  use_count         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope, id);
"""

_SCHEMA_V6_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  DELETE FROM facts_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text ON facts BEGIN
  UPDATE facts_fts SET text = new.text WHERE rowid = new.id;
END;
"""


def now_iso() -> str:
    """ISO-8601 UTC, second precision, e.g. 2026-11-03T18:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.fts_enabled = True   # set False by _migrate when SQLite has no FTS5
        self._migrate()

    def close(self):
        with self._lock:
            self.db.close()

    # ── schema migrations ──────────────────────────────────────────────
    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.db.executescript(_SCHEMA_V1)
            self.db.execute("PRAGMA user_version=1")
            self.db.commit()
        if version < 2:  # voice round: per-persona voice + voice_version
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(personas)")}
            if "voice" not in cols:
                self.db.execute("ALTER TABLE personas ADD COLUMN voice TEXT NOT NULL DEFAULT ''")
            if "voice_version" not in cols:
                self.db.execute("ALTER TABLE personas ADD COLUMN voice_version INTEGER NOT NULL DEFAULT 1")
            # personas written before the voice round get a generic design voice
            rows = self.db.execute("SELECT id, voice FROM personas").fetchall()
            for row in rows:
                if not row["voice"]:
                    self.db.execute("UPDATE personas SET voice=? WHERE id=?",
                                    (json.dumps(default_voice()), row["id"]))
            self.db.execute("PRAGMA user_version=2")
            self.db.commit()
        if version < 3:  # emotion + tools round (docs/ROUNDS.md R1-B)
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(personas)")}
            if "emotes" not in cols:
                # ALTER with a DEFAULT fills existing rows with 1: emotes are
                # on by default for personas written before this round.
                self.db.execute("ALTER TABLE personas ADD COLUMN emotes INTEGER NOT NULL DEFAULT 1")
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
            if "emotion" not in cols:
                self.db.execute("ALTER TABLE messages ADD COLUMN emotion TEXT")
            if "tools" not in cols:
                self.db.execute("ALTER TABLE messages ADD COLUMN tools TEXT NOT NULL DEFAULT ''")
            self.db.executescript(_SCHEMA_V3)
            self.db.execute("PRAGMA user_version=3")
            self.db.commit()
        if version < 4:  # embodiment round (docs/BODY.md)
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(personas)")}
            if "appearance" not in cols:
                self.db.execute("ALTER TABLE personas ADD COLUMN appearance TEXT NOT NULL DEFAULT ''")
            if "embodied" not in cols:
                # embodied defaults ON: pre-round personas act embodied
                # whenever an embodied client is online.
                self.db.execute("ALTER TABLE personas ADD COLUMN embodied INTEGER NOT NULL DEFAULT 1")
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
            if "actions" not in cols:
                self.db.execute("ALTER TABLE messages ADD COLUMN actions TEXT NOT NULL DEFAULT ''")
            self.db.execute("PRAGMA user_version=4")
            self.db.commit()
        if version < 5:  # spoken-text round: the cleaned TTS text of a reply
            # (hub/speech.py). Display keeps `content`; clients doing their
            # own TTS speak `speech`. Old rows stay '' and get cleaned on read.
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
            if "speech" not in cols:
                self.db.execute("ALTER TABLE messages ADD COLUMN speech TEXT NOT NULL DEFAULT ''")
            self.db.execute("PRAGMA user_version=5")
            self.db.commit()
        if version < 6:  # memory round (docs/WALK_MEMORY.md M)
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
            if "memory_writes" not in cols:
                # JSON [{id,text}]: facts this assistant reply saved, for the
                # 🧠 remembered chip
                self.db.execute("ALTER TABLE messages ADD COLUMN memory_writes "
                                "TEXT NOT NULL DEFAULT ''")
            self.db.executescript(_SCHEMA_V6)
        # FTS5 is best-effort: a SQLite without the module keeps the hub on
        # LIKE search (search_facts branches on self.fts_enabled). When the
        # index first appears on a DB that already has facts (e.g. this
        # Python grew FTS5 support), it is backfilled from the table.
        had_fts = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='facts_fts'").fetchone() is not None
        try:
            self.db.executescript(_SCHEMA_V6_FTS)
            self.fts_enabled = True
            if not had_fts:
                self.db.execute("DELETE FROM facts_fts")
                self.db.execute("INSERT INTO facts_fts(rowid, text) "
                                "SELECT id, text FROM facts")
        except sqlite3.OperationalError:
            self.fts_enabled = False
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    # ── state version & active persona ─────────────────────────────────
    def _meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def version(self) -> int:
        with self._lock:
            return int(self._meta("version", "0"))

    def bump_version(self) -> int:
        with self._lock:
            v = int(self._meta("version", "0")) + 1
            self._set_meta("version", v)
            self.db.commit()
            return v

    def set_version(self, value: int):
        with self._lock:
            self._set_meta("version", int(value))
            self.db.commit()

    def active(self) -> str:
        with self._lock:
            return self._meta("active", "")

    def set_active(self, persona_id: str) -> bool:
        """Returns True when the active persona actually changed."""
        with self._lock:
            if self._meta("active", "") == persona_id:
                return False
            self._set_meta("active", persona_id)
            self.db.commit()
            return True

    # ── personas ────────────────────────────────────────────────────────
    @staticmethod
    def _persona_dict(row) -> dict:
        try:
            voice = json.loads(row["voice"]) if row["voice"] else default_voice()
        except (ValueError, TypeError):
            voice = default_voice()
        return {
            "id": row["id"],
            "name": row["name"],
            "keywords": json.loads(row["keywords"]),
            "system_prompt": row["system_prompt"],
            "greeting": row["greeting"],
            "tts_voice": row["tts_voice"],
            "emotes": bool(row["emotes"]),
            "appearance": row["appearance"],
            "embodied": bool(row["embodied"]),
            "voice": voice,
            "voice_version": row["voice_version"],
            "avatar_version": row["avatar_version"],
            "has_avatar": bool(row["has_avatar"]),
            "updated_at": row["updated_at"],
        }

    def list_personas(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM personas ORDER BY rowid").fetchall()
        return [self._persona_dict(r) for r in rows]

    def get_persona(self, persona_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
        return self._persona_dict(row) if row else None

    def persona_exists(self, persona_id: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM personas WHERE id=?", (persona_id,)).fetchone() is not None

    def add_persona(self, pid: str, name: str, keywords: list[str], system_prompt: str,
                    greeting: str, tts_voice: str, voice: dict | None = None,
                    emotes: bool = True, appearance: str = "",
                    embodied: bool = True) -> dict:
        ts = now_iso()
        if voice is None:
            voice = default_voice()
        with self._lock:
            self.db.execute(
                "INSERT INTO personas(id,name,keywords,system_prompt,greeting,tts_voice,"
                "avatar_version,has_avatar,created_at,updated_at,voice,voice_version,emotes,"
                "appearance,embodied) "
                "VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?,?,?)",
                (pid, name, json.dumps(keywords), system_prompt, greeting, tts_voice,
                 ts, ts, json.dumps(voice), 1, 1 if emotes else 0,
                 str(appearance or "")[:1000], 1 if embodied else 0))
            self.db.commit()
        persona = self.get_persona(pid)
        assert persona is not None
        return persona

    def update_persona(self, pid: str, fields: dict) -> dict | None:
        sets, vals = [], []
        for key in ("name", "system_prompt", "greeting", "tts_voice"):
            if key in fields:
                sets.append(f"{key}=?")
                vals.append(fields[key])
        if "emotes" in fields:
            sets.append("emotes=?")
            vals.append(1 if fields["emotes"] else 0)
        if "appearance" in fields:
            sets.append("appearance=?")
            vals.append(str(fields["appearance"] or "")[:1000])
        if "embodied" in fields:
            sets.append("embodied=?")
            vals.append(1 if fields["embodied"] else 0)
        if "keywords" in fields:
            sets.append("keywords=?")
            vals.append(json.dumps(fields["keywords"]))
        with self._lock:
            if sets:
                sets.append("updated_at=?")
                vals.append(now_iso())
                self.db.execute(f"UPDATE personas SET {','.join(sets)} WHERE id=?", (*vals, pid))
                self.db.commit()
        return self.get_persona(pid)

    def delete_persona(self, pid: str) -> list[int]:
        """Delete a persona, its messages and its private notes. Returns the
        ids of the persona:<pid> facts that went away (memory round, so the
        server can emit memory_changed for each)."""
        with self._lock:
            scope = f"persona:{pid}"
            ids = [r["id"] for r in self.db.execute(
                "SELECT id FROM facts WHERE scope=?", (scope,)).fetchall()]
            self.db.execute("DELETE FROM facts WHERE scope=?", (scope,))
            self.db.execute("DELETE FROM personas WHERE id=?", (pid,))
            self.db.execute("DELETE FROM messages WHERE persona=?", (pid,))
            self.db.commit()
            return ids

    def bump_avatar(self, pid: str) -> dict | None:
        with self._lock:
            self.db.execute("UPDATE personas SET avatar_version=avatar_version+1, "
                            "has_avatar=1, updated_at=? WHERE id=?", (now_iso(), pid))
            self.db.commit()
        return self.get_persona(pid)

    def set_voice(self, pid: str, voice: dict, bump_version: bool = True) -> dict | None:
        """Store the persona's voice object; voice_version +1 on a real change
        (audio caches key on it, so every edit must bump)."""
        with self._lock:
            if bump_version:
                self.db.execute("UPDATE personas SET voice=?, voice_version=voice_version+1, "
                                "updated_at=? WHERE id=?",
                                (json.dumps(voice), now_iso(), pid))
            else:
                self.db.execute("UPDATE personas SET voice=?, updated_at=? WHERE id=?",
                                (json.dumps(voice), now_iso(), pid))
            self.db.commit()
        return self.get_persona(pid)

    # ── messages ────────────────────────────────────────────────────────
    def add_message(self, persona: str, role: str, content: str, source: str,
                    emotion: str | None = None, tools: list | None = None,
                    actions: list | None = None, speech: str = "",
                    memory_writes: list | None = None) -> int:
        """`emotion` only makes sense on assistant messages (docs/ROUNDS.md:
        null for user messages). `tools` is the compact tools_used list
        [{tool, label}] stored on the assistant reply. `actions` is the
        parsed body-action list [{name, at}] (docs/BODY.md). `speech` is the
        cleaned spoken text of the reply (hub/speech.py); '' on user rows.
        `memory_writes` is [{id, text}] — facts this reply saved via the
        remember tool (docs/WALK_MEMORY.md M, the 🧠 chip)."""
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO messages(persona,role,content,source,created_at,emotion,"
                "tools,actions,speech,memory_writes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (persona, role, content, source, now_iso(), emotion,
                 json.dumps(tools) if tools else "",
                 json.dumps(actions) if actions else "",
                 str(speech or ""),
                 json.dumps(memory_writes) if memory_writes else ""))
            self.db.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def get_message(self, message_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._message_dict(row) if row else None

    def recent_messages(self, persona: str, limit: int) -> list[dict]:
        """The LAST `limit` messages of a persona, oldest -> newest."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE persona=? ORDER BY id DESC LIMIT ?",
                (persona, limit)).fetchall()
        return [self._message_dict(r) for r in reversed(rows)]

    def messages_after(self, persona: str, after: int, limit: int) -> list[dict]:
        """after=0 -> the last `limit` messages; otherwise ids > after. Oldest -> newest."""
        if after <= 0:
            return self.recent_messages(persona, limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE persona=? AND id>? ORDER BY id LIMIT ?",
                (persona, after, limit)).fetchall()
        return [self._message_dict(r) for r in rows]

    @staticmethod
    def _message_dict(row) -> dict:
        tools = []
        if row["tools"]:
            try:
                tools = json.loads(row["tools"])
            except ValueError:
                tools = []
        actions = []
        if row["actions"]:
            try:
                actions = json.loads(row["actions"])
            except ValueError:
                actions = []
        speech = row["speech"] if "speech" in row.keys() else ""
        mem_writes = []
        raw_mw = row["memory_writes"] if "memory_writes" in row.keys() else ""
        if raw_mw:
            try:
                mem_writes = json.loads(raw_mw)
            except ValueError:
                mem_writes = []
        return {"id": row["id"], "persona": row["persona"], "role": row["role"],
                "content": row["content"], "source": row["source"],
                "emotion": row["emotion"], "tools_used": tools,
                "actions": actions, "speech": speech,
                "memory_writes": mem_writes,
                "created_at": row["created_at"]}

    def clear_messages(self, persona: str) -> int:
        with self._lock:
            cur = self.db.execute("DELETE FROM messages WHERE persona=?", (persona,))
            self.db.commit()
            return cur.rowcount

    def message_ids(self, persona: str) -> list[int]:
        """Every stored message id of a persona (so its cached audio can be
        removed before the rows go away)."""
        with self._lock:
            rows = self.db.execute("SELECT id FROM messages WHERE persona=?", (persona,)).fetchall()
        return [r["id"] for r in rows]

    def last_message_id(self) -> int:
        with self._lock:
            row = self.db.execute("SELECT MAX(id) AS m FROM messages").fetchone()
        return row["m"] or 0

    # ── long-term memory facts (docs/WALK_MEMORY.md M) ─────────────────
    # scope: "user" (shared across personas) or "persona:<id>" (private note)
    def _fact_dict(self, row) -> dict:
        return {"id": row["id"], "text": row["text"], "scope": row["scope"],
                "pinned": bool(row["pinned"]),
                "source_message_id": row["source_message_id"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "last_used_at": row["last_used_at"], "use_count": row["use_count"]}

    def add_fact(self, text: str, scope: str = "user", pinned: bool = False,
                 source_message_id: int | None = None) -> dict:
        """Insert a fact verbatim (no dedup here — upsert_fact dedups)."""
        text = _memory.clamp_fact_text(text)
        ts = now_iso()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO facts(text,scope,pinned,source_message_id,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?)",
                (text, scope, 1 if pinned else 0, source_message_id, ts, ts))
            new_id = cur.lastrowid
            self.db.commit()
        assert new_id is not None
        return self.get_fact(new_id) or {}

    def upsert_fact(self, text: str, scope: str = "user", pinned: bool = False,
                    source_message_id: int | None = None):
        """remember-tool semantics: near-identical text in the same scope
        updates the existing fact instead of duplicating it. Returns
        (fact, updated_existing)."""
        text = _memory.clamp_fact_text(text)
        norm = _memory.normalize_text(text)
        with self._lock:
            existing = None
            keep_pinned = False
            for row in self.db.execute(
                    "SELECT id, text, pinned FROM facts WHERE scope=? ORDER BY id",
                    (scope,)):
                if _memory.similar(norm, _memory.normalize_text(row["text"])) >= 0.92:
                    existing = row["id"]
                    keep_pinned = bool(row["pinned"])
                    break
            updated = existing is not None
            if existing is not None:
                self.db.execute(
                    "UPDATE facts SET text=?, pinned=?, updated_at=? WHERE id=?",
                    (text, 1 if (pinned or keep_pinned) else 0, now_iso(), existing))
            else:
                cur = self.db.execute(
                    "INSERT INTO facts(text,scope,pinned,source_message_id,created_at,"
                    "updated_at) VALUES(?,?,?,?,?,?)",
                    (text, scope, 1 if pinned else 0, source_message_id,
                     now_iso(), now_iso()))
                existing = cur.lastrowid
            self.db.commit()
        return self.get_fact(existing or 0) or {}, updated

    def get_fact(self, fact_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM facts WHERE id=?",
                                  (fact_id,)).fetchone()
        return self._fact_dict(row) if row else None

    def list_facts(self, scopes: list[str] | None = None, limit: int = 200,
                   offset: int = 0) -> tuple[list[dict], int]:
        """Facts newest-first (updated_at, then id). scopes=None = every scope.
        Returns (page, total)."""
        where, params = "", []
        if scopes is not None:
            if not scopes:
                return [], 0
            where = "WHERE scope IN (" + ",".join("?" * len(scopes)) + ")"
            params = list(scopes)
        with self._lock:
            total = self.db.execute(
                f"SELECT COUNT(*) AS c FROM facts {where}", params).fetchone()["c"]
            rows = self.db.execute(
                f"SELECT * FROM facts {where} ORDER BY updated_at DESC, id DESC "
                "LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return [self._fact_dict(r) for r in rows], total

    def search_facts(self, query: str, scopes: list[str] | None = None,
                     limit: int = 10) -> list[dict]:
        """FTS5 OR-match over the query's words, ranked (bm25); SQLite without
        FTS5 (or a query with no usable words) falls back to LIKE. Scoped."""
        match = _memory.fts_match(query)
        scope_sql, scope_params = "", []
        if scopes is not None:
            if not scopes:
                return []
            scope_sql = "AND f.scope IN (" + ",".join("?" * len(scopes)) + ")"
            scope_params = list(scopes)
        with self._lock:
            if self.fts_enabled and match:
                rows = self.db.execute(
                    "SELECT f.*, bm25(facts_fts) AS score FROM facts_fts "
                    "JOIN facts f ON f.id = facts_fts.rowid "
                    f"WHERE facts_fts MATCH ? {scope_sql} "
                    "ORDER BY score LIMIT ?", [match] + scope_params + [limit]).fetchall()
                return [self._fact_dict(r) for r in rows]
            like = f"%{_memory.clamp_fact_text(query).lower()}%"
            rows = self.db.execute(
                "SELECT f.* FROM facts f WHERE lower(f.text) LIKE ? "
                f"{scope_sql} ORDER BY f.updated_at DESC LIMIT ?",
                [like] + scope_params + [limit]).fetchall()
        return [self._fact_dict(r) for r in rows]

    def recent_facts(self, scopes: list[str], exclude_ids: set[int],
                     limit: int = 10) -> list[dict]:
        """Facts by recency (last_used first, then updated) for the prompt
        block's fill tier."""
        if not scopes:
            return []
        excl = ",".join("?" * len(exclude_ids)) if exclude_ids else None
        params = list(scopes)
        where = "scope IN (" + ",".join("?" * len(scopes)) + ")"
        if excl:
            where += f" AND id NOT IN ({excl})"
            params += sorted(exclude_ids)
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM facts WHERE {where} ORDER BY "
                "COALESCE(last_used_at,'') DESC, updated_at DESC, id DESC LIMIT ?",
                params + [limit]).fetchall()
        return [self._fact_dict(r) for r in rows]

    def update_fact(self, fact_id: int, text: str | None = None,
                    pinned: bool | None = None) -> dict | None:
        """Edit a fact's text and/or pin. Returns None when the id is unknown."""
        with self._lock:
            if not self.db.execute("SELECT 1 FROM facts WHERE id=?",
                                   (fact_id,)).fetchone():
                return None
            if text is not None:
                self.db.execute("UPDATE facts SET text=?, updated_at=? WHERE id=?",
                                (_memory.clamp_fact_text(text), now_iso(), fact_id))
            if pinned is not None:
                self.db.execute("UPDATE facts SET pinned=?, updated_at=? WHERE id=?",
                                (1 if pinned else 0, now_iso(), fact_id))
            self.db.commit()
        return self.get_fact(fact_id)

    def delete_fact(self, fact_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self.db.commit()
            return cur.rowcount > 0

    def delete_facts_by_scope(self, scope: str) -> list[int]:
        """Forget everything in one scope ('user' or 'persona:<id>')."""
        with self._lock:
            ids = [r["id"] for r in self.db.execute(
                "SELECT id FROM facts WHERE scope=?", (scope,)).fetchall()]
            self.db.execute("DELETE FROM facts WHERE scope=?", (scope,))
            self.db.commit()
            return ids

    def delete_all_facts(self) -> list[int]:
        with self._lock:
            ids = [r["id"] for r in self.db.execute("SELECT id FROM facts").fetchall()]
            self.db.execute("DELETE FROM facts")
            self.db.commit()
            return ids

    def touch_facts(self, fact_ids: list[int]):
        """Usage bump for facts that entered a prompt block."""
        if not fact_ids:
            return
        ts = now_iso()
        with self._lock:
            self.db.executemany(
                "UPDATE facts SET last_used_at=?, use_count=use_count+1 WHERE id=?",
                [(ts, i) for i in fact_ids])
            self.db.commit()

    def export_facts(self) -> list[dict]:
        rows, _ = self.list_facts(scopes=None, limit=1_000_000, offset=0)
        return rows

    # ── settings (JSON blobs under named keys) ─────────────────────────
    def get_json(self, key: str, default):
        with self._lock:
            raw = self._meta_json(key)
        return default if raw is None else json.loads(raw)

    def _meta_json(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_json(self, key: str, value) -> None:
        with self._lock:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()

    def has_json(self, key: str) -> bool:
        with self._lock:
            return self._meta_json(key) is not None

    def del_json(self, key: str) -> bool:
        """Delete a stored settings key; True when one existed (an override
        really went away, so callers know to bump the state version)."""
        with self._lock:
            cur = self.db.execute("DELETE FROM settings WHERE key=?", (key,))
            self.db.commit()
            return cur.rowcount > 0

    # ── tool log (GET /api/tools/log) ───────────────────────────────────
    def log_tool(self, persona: str, tool: str, source: str, arguments: str,
                 ok: bool, error: str, ms: int, approval: str = "") -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO tool_log(created_at,persona,tool,source,arguments,ok,error,"
                "ms,approval) VALUES(?,?,?,?,?,?,?,?,?)",
                (now_iso(), persona, tool, source, str(arguments or "")[:500],
                 1 if ok else 0, str(error or "")[:500], int(ms), approval or ""))
            # keep the table small without a scheduler: trim on write
            self.db.execute(
                "DELETE FROM tool_log WHERE id <= (SELECT MAX(id) FROM tool_log) - ?",
                (_TOOL_LOG_KEEP,))
            self.db.commit()

    def tool_log(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM tool_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "created_at": r["created_at"], "persona": r["persona"],
                 "tool": r["tool"], "source": r["source"], "arguments": r["arguments"],
                 "ok": bool(r["ok"]), "error": r["error"], "ms": r["ms"],
                 "approval": r["approval"]} for r in rows]
