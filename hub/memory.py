"""Long-term memory helpers (docs/WALK_MEMORY.md M).

Pure logic over the store's `facts` table: FTS query building, text
normalisation for dedup, token estimation and the "What you remember"
prompt block (pinned facts first, then FTS matches for the user's message,
then most recently used, capped at a token budget). No imports from the
server or the store — store.py imports this module, not the other way round.
"""
import re
import unicodedata

MAX_FACT_CHARS = 500
# rough token estimate for the prompt budget: no tokenizer on the hub,
# ~4 chars/token is the usual heuristic for English chat text
CHARS_PER_TOKEN = 4

_WORD_RE = re.compile(r"[A-Za-z0-9\u00c0-\uffff']+")
_WS_RE = re.compile(r"\s+")


def clamp_fact_text(text) -> str:
    """Clean a fact: collapse whitespace, cap at MAX_FACT_CHARS."""
    return _WS_RE.sub(" ", str(text or "")).strip()[:MAX_FACT_CHARS]


def normalize_text(text) -> str:
    """Case/mark/space-insensitive compare form used for dedup."""
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _WS_RE.sub(" ", s).strip().rstrip(".!?,;:")


def similar(a: str, b: str) -> float:
    """0..1 similarity of two normalized fact texts."""
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # a fact fully contained in the other is near-identical for dedup purposes
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b)) * 0.9 + 0.1
    return SequenceMatcher(None, a, b).ratio()


def fts_tokens(query) -> list[str]:
    """Words usable in an FTS5 MATCH (doubled quotes survive, caps apply)."""
    toks = []
    for w in _WORD_RE.findall(str(query or "").lower()):
        w = w.replace("'", "''")
        if w and w not in toks:
            toks.append(w)
        if len(toks) >= 12:
            break
    return toks


def fts_match(query) -> str:
    """An OR-match over the query's words; empty string = no usable tokens."""
    return " OR ".join(f'"{w}"' for w in fts_tokens(query))


def est_tokens(text) -> int:
    return max(1, len(str(text)) // CHARS_PER_TOKEN)


MEMORY_HEADER = "What you remember (facts saved in past conversations):"


def memory_instruction(auto_remember: bool, tools_offered: bool) -> str:
    instr = ("Use these naturally — the user may have told you this a while "
             "ago; if they say something changed, believe them.")
    if tools_offered:
        if auto_remember:
            instr += (" When the user shares something lasting about themselves "
                      "(name, preferences, projects, people, dates), call "
                      "remember. Don't store secrets, passwords or one-off "
                      "trivia. When asked to forget something, call forget_fact.")
        else:
            instr += (" Only call remember when the user explicitly asks you to "
                      "store something, never on your own. Don't store secrets "
                      "or passwords. When asked to forget something, call "
                      "forget_fact.")
    else:
        instr += (" These notes were saved earlier; you cannot change them in "
                  "this chat (memory tools are off).")
    return instr


def build_memory_block(facts, auto_remember: bool, tools_offered: bool,
                       budget_tokens: int):
    """Assemble the prompt block from already-ordered fact dicts.

    Facts fill the budget in the order given (pinned, then matches, then
    recent); lines that would overflow the budget are skipped. Returns
    (block_text, used_facts) — ("", []) when no fact fits.
    """
    instr = memory_instruction(auto_remember, tools_offered)
    used = est_tokens(MEMORY_HEADER) + est_tokens(instr) + 2
    lines, picked = [], []
    for f in facts:
        line = f"- {f['text']}" + (" (pinned)" if f.get("pinned") else "")
        cost = est_tokens(line) + 1
        if used + cost > budget_tokens:
            continue
        used += cost
        lines.append(line)
        picked.append(f)
    if not picked:
        return "", []
    return MEMORY_HEADER + "\n" + "\n".join(lines) + "\n" + instr, picked


def gather_facts(store, user_text: str, persona_id: str):
    """Prompt-block ordering (docs/WALK_MEMORY.md M): all pinned facts
    first, then FTS matches for the user's message, then most recently
    used. Deduped, newest tiers keep their rank. `store` is duck-typed
    (list_facts / search_facts / recent_facts)."""
    scopes = ["user", f"persona:{persona_id}"]
    listed, _ = store.list_facts(scopes=scopes, limit=500)
    ordered, seen = [], set()
    for f in listed:
        if f["pinned"] and f["id"] not in seen:
            ordered.append(f)
            seen.add(f["id"])
    for f in store.search_facts(user_text, scopes=scopes, limit=10):
        if f["id"] not in seen:
            ordered.append(f)
            seen.add(f["id"])
    for f in store.recent_facts(scopes, seen, limit=10):
        if f["id"] not in seen:
            ordered.append(f)
            seen.add(f["id"])
    return ordered
