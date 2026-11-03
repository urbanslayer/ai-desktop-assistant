"""Emotion tags: the fixed set, the prompt rule, parsing and TTS delivery hints.

Contract (docs/ROUNDS.md R1-B): a persona with `emotes` true gets a short
system-prompt line telling her to start every reply with one tag like
`[teasing]`. The hub strips the tag — tolerating `(teasing)`, a missing tag
or an unknown tag (-> `neutral`) — and never leaves a tag in the stored or
spoken text. The parsed emotion rides on the assistant Message and the
ChatResult, keys the Breeze delivery hint and the audio cache key.
"""
import re

# Fixed set — clients (R1-A) map these to VRM expressions.
EMOTIONS = ["neutral", "happy", "teasing", "annoyed", "sad", "surprised",
            "thinking", "embarrassed"]

# Appended to the system prompt when persona.emotes is true.
PROMPT_RULE = (
    "Emotion: start EVERY reply with exactly one emotion tag in square "
    "brackets, one of [neutral] [happy] [teasing] [annoyed] [sad] "
    "[surprised] [thinking] [embarrassed], then your reply on the same "
    'line. Example: "[teasing] Sure thing, senpai." Never use an emoji, '
    "and never repeat the tag later in the reply."
)

# Breeze delivery direction per emotion (docs/VOICE.md `instructions`).
# neutral adds nothing: her default delivery is already neutral.
TTS_HINTS = {
    "neutral": "",
    "happy": "cheerful, upbeat and warm",
    "teasing": "playful, teasing tone",
    "annoyed": "irritated, clipped",
    "sad": "soft, low and melancholy",
    "surprised": "startled, wide-eyed and quick",
    "thinking": "slow, thoughtful, musing",
    "embarrassed": "shy, soft and a little flustered",
}


def tts_hint(emotion: str | None) -> str:
    return TTS_HINTS.get(str(emotion or "neutral"), "")


# A leading bracket/paren/angle tag whose body is one short word: exactly the
# shape an emotion tag comes in, in the wild (`[teasing]`, `(annoyed)`, …).
_TAG = re.compile(r"^\s*[\[({<](?P<tag>[A-Za-z][A-Za-z _-]{0,23})[\])}>]\s*")


def parse_emotion(text: str) -> tuple[str, str]:
    """Split a leading emotion tag off a model reply.

    Returns (emotion, cleaned_text). Missing or unknown tag -> "neutral".
    The tag never survives into the cleaned text — except an unknown
    bracket token immediately followed by `(`, which is a markdown link
    (`[text](url)`) at the start of a reply, not a tag: it stays put.
    """
    raw = text or ""
    m = _TAG.match(raw)
    if not m:
        return "neutral", raw.strip()
    tag = m.group("tag").strip().lower().replace("_", " ").replace("-", " ")
    tag = " ".join(tag.split())  # "happy " / "happy  " -> "happy"
    rest = raw[m.end():].strip()
    if tag in EMOTIONS:
        return tag, rest
    # unknown tag -> neutral; strip it too ("never leave a tag in the text"),
    # but keep real markdown links: `[title](url)` has no space before `(`.
    if raw[m.end():].startswith("("):
        return "neutral", raw.strip()
    return "neutral", rest
