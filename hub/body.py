"""Body actions: the embodiment contract (docs/BODY.md).

When an embodied client (the desktop avatar) is online it registers a
`body.actions` list; the **body set** is the union over the online clients.
A persona with `embodied` true then gets a short prompt block naming those
actions; the model may pepper its reply with `[wave]`, `[action:think]` or
roleplay asterisks like `*waves*`. This module turns such a reply into
clean text + a structured `actions` list (docs/API.md *Body actions*).

Rules (docs/BODY.md §3, applied AFTER emotion-tag stripping):

* `[name]` / `[action:name]` whose normalised name is in the body set →
  stripped, recorded as an action with an `at` fraction (the character
  offset where it appeared, divided by the cleaned reply's length).
* `*waves*`-style asterisks map through a small verb table and go too.
* An action request for a name NOT in the body set (explicit `[action:x]`
  or a known gesture name like `fidget`) is also stripped and reported in
  `ignored_actions` — the spoken text stays clean. So is an asterisk verb
  whose action cannot play.
* Any other bracket tag (a mid-text emotion tag, a markdown link,
  `[note to self]`, …) is left untouched.
* At most MAX_ACTIONS per reply (extras dropped); consecutive duplicates
  merge. `appearance` (persona text ≤ 1000 chars) rides the prompt even
  with no body so she can describe herself in text-only chats.
"""
import logging
import re

log = logging.getLogger("riko.hub.body")

# The desktop's built-in gesture vocabulary (docs/BODY.md §1). Names outside
# the current body set that match this table are *action requests* (stripped
# and logged as ignored), unlike an arbitrary bracket tag, which stays put.
GESTURE_VOCAB = ["wave", "think", "nod", "shake_head", "look_around",
                 "stretch", "fidget", "hair_touch", "laugh", "pout",
                 "listen"]

# Roleplay asterisk verbs -> canonical action (docs/BODY.md §3). Base, -s and
# -ing forms all map; keys are compared against normalised inner text.
ACTION_VERBS = {
    "wave": "wave", "waves": "wave", "waving": "wave",
    "nod": "nod", "nods": "nod", "nodding": "nod",
    "shake head": "shake_head", "shakes head": "shake_head",
    "shaking head": "shake_head", "shook head": "shake_head",
    "look around": "look_around", "looks around": "look_around",
    "looking around": "look_around",
    "stretch": "stretch", "stretches": "stretch", "stretching": "stretch",
    "laugh": "laugh", "laughs": "laugh", "laughing": "laugh",
    "pout": "pout", "pouts": "pout", "pouting": "pout",
    "touch hair": "hair_touch", "touches hair": "hair_touch",
    "touching hair": "hair_touch", "think": "think", "thinks": "think",
    "thinking": "think",
}

MAX_ACTIONS = 3
MAX_APPEARANCE_CHARS = 1000

# The prompt block (docs/BODY.md §2), ~110 tokens with a full action list.
PROMPT_TEMPLATE = (
    "You have a 3D body: you stand on the user's desktop and they can see "
    "you. You can move by putting action tags anywhere in your reply, e.g. "
    "[wave], [think], [nod]. Available actions: {names}. When the user asks "
    "you to do one, do it (and you may say a few words about it). Otherwise "
    "use them sparingly and naturally, like a person gesturing while "
    "talking; usually zero or one per reply. Never describe actions in "
    "prose like *waves*; use the tags."
)


def normalize_action_name(raw) -> str:
    """`Shake-Head` / `shake head` / `shake_head` -> `shake_head` ('' when empty)."""
    return re.sub(r"[\s\-]+", "_", str(raw or "").strip().lower()).strip("_")


def body_prompt(actions: list[str]) -> str:
    """The embodiment prompt block naming the current body set."""
    return PROMPT_TEMPLATE.format(names=", ".join(actions))


def appearance_prompt(appearance: str) -> str:
    return "Your appearance: " + str(appearance or "").strip()


# Bracket tags: `[wave]`, `[action:wave]`, `[shake head]`. `[a](b)` (a
# markdown link) is detected at match time and skipped. Asterisk spans:
# one or two stars, a short phrase, no newlines inside.
_BRACKET = re.compile(r"\[\s*([^\[\]\n]{1,40}?)\s*\]")
_ASTERISK = re.compile(r"\*{1,2}([^*\n]{1,40}?)\*{1,2}")


def _classify_bracket(inner: str, body_set: set[str]) -> tuple[str, bool] | None:
    """(canonical_name, playable) for a bracket tag body, or None to keep it.

    playable=True means the name is in the body set: strip + record. An
    explicit `[action:x]` or a bare tag naming a KNOWN gesture that the
    online clients cannot do is a request that cannot play: strip + ignore.
    Any other word (`[sad]`, `[note]`) is not an action request and stays in
    the text — that is how a mid-text emotion tag survives.
    """
    text = inner.strip()
    m = re.match(r"^action\s*[:\-]\s*(.+)$", text, re.IGNORECASE)
    if m:  # explicit `[action:name]`: always an action request
        name = normalize_action_name(m.group(1))
        if not name:
            return None
        canon = ACTION_VERBS.get(" ".join(name.split("_")), name)
        return canon, canon in body_set
    name = normalize_action_name(text)
    if not name:
        return None
    if name in body_set:
        return name, True
    if name in GESTURE_VOCAB:
        return name, False
    return None


def parse_actions(text: str, body_actions: list[str]) -> tuple[str, list[dict], list[str]]:
    """Split action tags out of an emotion-stripped reply.

    Returns (clean_text, actions, ignored). Each action is
    {"name": str, "at": float} with `at` the 0..1 fraction of the CLEANED
    reply's length where the tag sat. Unknown bracket tags that are not
    action requests stay untouched; action requests for names outside the
    body set are stripped into `ignored`.
    """
    body_set = {normalize_action_name(a) for a in body_actions or []}
    raw = text or ""
    kept: list[str] = []   # kept chars, in order
    spans: list[tuple[int, str]] = []  # (cleaned offset, action name)
    ignored: list[str] = []

    pieces: list[tuple[int, int, str | None]] = []  # (start, end, name|None|'ignore')
    for m in _BRACKET.finditer(raw):
        if raw[m.end():m.end() + 1] == "(":      # markdown link — hands off
            continue
        verdict = _classify_bracket(m.group(1), body_set)
        if verdict is None:
            continue
        name, playable = verdict
        if playable:
            pieces.append((m.start(), m.end(), name))
        else:
            ignored.append(name)
            pieces.append((m.start(), m.end(), None))

    def verb_hit(inner: str) -> tuple[str, bool] | None:
        key = " ".join(str(inner or "").strip().lower().split()).rstrip(" .!")
        canon = ACTION_VERBS.get(key)
        if canon is None:
            return None
        return canon, canon in body_set

    bracket_spans = {(s, e) for s, e, _ in pieces}
    for m in _ASTERISK.finditer(raw):
        if any(s < m.end() and m.start() < e for s, e in bracket_spans):
            continue                              # overlaps a bracket tag
        hit = verb_hit(m.group(1))
        if hit is None:
            continue                              # *smiles* stays verbatim
        canon, playable = hit
        if playable:
            pieces.append((m.start(), m.end(), canon))
        else:
            ignored.append(canon)
            pieces.append((m.start(), m.end(), None))

    pieces.sort(key=lambda p: p[0])
    pos = 0
    for start, end, name in pieces:
        kept.append(raw[pos:start])
        if name is not None:
            spans.append((sum(len(k) for k in kept), name))
        pos = end
    kept.append(raw[pos:])
    clean = _tidy("".join(kept))

    actions: list[dict] = []
    for offset, name in spans:
        at = round(offset / len(clean), 3) if clean else 0.0
        at = min(1.0, max(0.0, at))
        if actions and actions[-1]["name"] == name:
            continue                              # adjacent duplicate: merged
        actions.append({"name": name, "at": at})
    return clean, actions[:MAX_ACTIONS], ignored


def _tidy(text: str) -> str:
    """Clean text left behind by removed tags: no doubled spaces, no space
    before punctuation, no blank-line runs, trimmed."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    return text.strip()
