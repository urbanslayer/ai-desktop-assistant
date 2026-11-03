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

SCHEMA_VERSION = 3

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
                    emotes: bool = True) -> dict:
        ts = now_iso()
        if voice is None:
            voice = default_voice()
        with self._lock:
            self.db.execute(
                "INSERT INTO personas(id,name,keywords,system_prompt,greeting,tts_voice,"
                "avatar_version,has_avatar,created_at,updated_at,voice,voice_version,emotes) "
                "VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?)",
                (pid, name, json.dumps(keywords), system_prompt, greeting, tts_voice,
                 ts, ts, json.dumps(voice), 1, 1 if emotes else 0))
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

    def delete_persona(self, pid: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM personas WHERE id=?", (pid,))
            self.db.execute("DELETE FROM messages WHERE persona=?", (pid,))
            self.db.commit()

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
                    emotion: str | None = None, tools: list | None = None) -> int:
        """`emotion` only makes sense on assistant messages (docs/ROUNDS.md:
        null for user messages). `tools` is the compact tools_used list
        [{tool, label}] stored on the assistant reply."""
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO messages(persona,role,content,source,created_at,emotion,tools) "
                "VALUES(?,?,?,?,?,?,?)",
                (persona, role, content, source, now_iso(),
                 emotion, json.dumps(tools) if tools else ""))
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
        return {"id": row["id"], "persona": row["persona"], "role": row["role"],
                "content": row["content"], "source": row["source"],
                "emotion": row["emotion"], "tools_used": tools,
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
