"""Memory tools: remember / recall / forget_fact (docs/WALK_MEMORY.md M).

Hub-side, executed by the hub like web_search — permission defaults to
`always` (registry.permission_for), editable in Settings. They are built
per turn because they close over the responding persona (scope rules: user
facts are shared across personas; persona:<id> notes are private) and over
the turn's memory_writes sink — the facts a reply saves are stored on the
assistant message and shown as the 🧠 remembered chip.
"""
import json

try:  # package import (repo root) vs standalone (`python server.py` in hub/)
    from .. import memory as mem
except (ImportError, SystemError):  # standalone: hub/ is on sys.path
    import memory as mem  # type: ignore[no-redef]

MAX_FACTS_PER_TURN = 5

REMEMBER_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string",
                 "description": f"The fact, one sentence, ≤{mem.MAX_FACT_CHARS} chars"},
        "scope": {"type": "string", "enum": ["user", "persona"]},
        "pinned": {"type": "boolean",
                   "description": "Pinned facts always enter the prompt"},
    },
    "required": ["text"],
}
RECALL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Keywords, not a full question"},
        "limit": {"type": "integer"},
    },
    "required": ["query"],
}
FORGET_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
}
MEMORY_TOOL_SCHEMAS = {"remember": REMEMBER_SCHEMA, "recall": RECALL_SCHEMA,
                       "forget_fact": FORGET_SCHEMA}


def build_memory_specs(store, events, persona_id: str, writes: list,
                       auto_remember: bool = True) -> list:
    """remember/recall/forget_fact bound to one turn. ToolSpec comes from
    registry, imported here so registry -> tools.memory stays one-way."""
    from .registry import ToolSpec

    scopes = ["user", f"persona:{persona_id}"]

    def note(fact_id, op):
        try:
            events.publish("memory_changed", id=fact_id, op=op)
        except Exception:  # events must never break a tool call
            pass

    def _remember(args) -> str:
        raw = str((args or {}).get("text") or "").strip()
        if not raw:
            raise RuntimeError("tool_error:remember needs a text")
        if len(writes) >= MAX_FACTS_PER_TURN:
            raise RuntimeError(f"tool_error:memory limit reached "
                               f"({MAX_FACTS_PER_TURN} facts per turn); finish "
                               "the reply")
        scope = str(args.get("scope") or "user")
        if scope not in ("user", "persona"):
            raise RuntimeError("tool_error:scope must be 'user' or 'persona'")
        full_scope = scope if scope == "user" else f"persona:{persona_id}"
        # A persona note that duplicates a user fact is merged into the user
        # scope instead of being saved twice (reviewer fix: the model tends to
        # call remember twice with different scopes for one statement).
        dedup = ["user"] if full_scope != "user" else None
        fact, updated = store.upsert_fact(raw, scope=full_scope,
                                          pinned=bool(args.get("pinned")),
                                          dedup_scopes=dedup)
        note(fact.get("id"), "updated" if updated else "added")
        writes.append({"id": fact["id"], "text": fact["text"]})
        return json.dumps({"id": fact["id"], "scope": fact["scope"],
                           "updated": updated, "text": fact["text"]},
                          ensure_ascii=False)

    def _recall(args) -> str:
        query = str((args or {}).get("query") or "").strip()
        if not query:
            raise RuntimeError("tool_error:recall needs a query")
        try:
            limit = max(1, min(20, int(args.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        results = store.search_facts(query, scopes=scopes, limit=limit)
        if results:
            store.touch_facts([f["id"] for f in results])
        return json.dumps(
            {"count": len(results),
             "facts": [{"id": f["id"], "text": f["text"], "scope": f["scope"],
                        "pinned": f["pinned"]} for f in results]},
            ensure_ascii=False)

    def _forget(args) -> str:
        args = args or {}
        raw_id = args.get("id")
        if raw_id not in (None, ""):
            try:
                fact_id = int(raw_id)
            except (TypeError, ValueError):
                raise RuntimeError("tool_error:forget_fact id must be an "
                                   "integer") from None
            fact = store.get_fact(fact_id)
            if not fact or fact["scope"] not in scopes:
                return json.dumps({"error": f"no memory fact with id {fact_id}"})
            store.delete_fact(fact_id)
            note(fact_id, "deleted")
            return json.dumps({"deleted": fact_id, "text": fact["text"]},
                              ensure_ascii=False)
        text = str(args.get("text") or "").strip()
        if not text:
            raise RuntimeError("tool_error:forget_fact needs an id or a text")
        # "by text when it clearly matches one fact": an exact normalized
        # match wins outright; a single search hit may also be deleted;
        # anything ambiguous comes back as a candidate list so the model can
        # recall first and retry with the right id.
        norm = mem.normalize_text(text)
        candidates = store.search_facts(text, scopes=scopes, limit=5)
        exact = [f for f in candidates if mem.normalize_text(f["text"]) == norm]
        winner = exact[0] if exact else (candidates[0] if len(candidates) == 1 else None)
        if winner is None:
            return json.dumps(
                {"error": "no clear match — retry with an id",
                 "candidates": [{"id": f["id"], "text": f["text"],
                                 "scope": f["scope"]} for f in candidates]},
                ensure_ascii=False)
        store.delete_fact(winner["id"])
        note(winner["id"], "deleted")
        return json.dumps({"deleted": winner["id"], "text": winner["text"]},
                          ensure_ascii=False)

    auto = ("\nCall this on your own when the user shares something lasting "
            "about themselves (name, preferences, projects, people, dates). "
            if auto_remember else
            "\nCall this only when the user explicitly asks you to remember "
            "something. ")
    return [
        ToolSpec(
            name="remember", display="remember",
            description=("Save one lasting fact to long-term memory so it "
                         "survives across conversations." + auto +
                         "Never store secrets, passwords or one-off trivia. "
                         "scope 'user' = about the user, shared with every "
                         "persona (default). scope 'persona' = a private note "
                         "only this persona sees. Repeating a known fact "
                         "updates it instead of duplicating it."),
            parameters=REMEMBER_SCHEMA,
            source="hub", risk="act", run=_remember,
            label=lambda args: "remembering: " + str((args or {}).get("text") or "")[:40],
        ),
        ToolSpec(
            name="recall", display="recall",
            description=("Search long-term memory by keywords. Returns "
                         "[{id, text, scope, pinned}] — facts about the user "
                         "(shared) and this persona's private notes. Use it "
                         "before claiming you don't know something, and to "
                         "find a fact's id before forgetting it."),
            parameters=RECALL_SCHEMA,
            source="hub", risk="read", run=_recall,
            label=lambda args: "searching memory for “" + str((args or {}).get("query") or "")[:40] + "”",
        ),
        ToolSpec(
            name="forget_fact", display="forget_fact",
            description=("Delete a fact from long-term memory, by id or by "
                         "its exact text when it clearly matches one fact. "
                         "Ambiguous text returns a candidate list — recall "
                         "first, then retry with the id."),
            parameters=FORGET_SCHEMA,
            source="hub", risk="act", run=_forget,
            label=lambda args: ("forgetting fact #" + str((args or {}).get("id"))
                                if (args or {}).get("id") not in (None, "")
                                else "forgetting: " + str((args or {}).get("text") or "")[:40]),
        ),
    ]
