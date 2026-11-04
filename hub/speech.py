"""What she *says* vs what she *shows*: turn a stored reply into TTS text.

Her replies carry markdown, links and citations that look fine in a bubble
but sound awful read aloud ("https colon slash slash..."). The displayed
text keeps everything; the AUDIO gets this cleaned text instead:

  * fenced code blocks and inline code spans are dropped (nobody dictates code),
  * markdown images are dropped; markdown links keep only the label,
  * bare http(s):// and www. URLs are dropped,
  * bracketed/parenthetical source citations like [1], [2, 3] or (source: x) go,
  * markdown furniture goes: heading '#', '>' quotes, '-'/ '*' bullets,
    emphasis '**' '*' '_' '~~', horizontal rules (the WORDS all stay),
  * emoji and other pictographs are dropped (breeze reads their names),
  * per docs/TOOLS.md A the spoken answer is the part before the first blank
    line: when the reply has a details/links section after one, only the first
    paragraph is spoken. If that first paragraph has nothing speakable left
    (it was all links), a short fallback line is spoken instead — a reply
    with words never becomes empty audio.

`riko/speech.py` is the client-side mirror (the hub deploys standalone and the
client must clean identically for tts: openai/sovits/fish). The two files are
kept in lockstep by the shared table tests/data/speech_cases.json.

SPEECH_RULES_VERSION goes into every audio cache filename: bump it whenever
these rules change so cached clips synthesized under the old rules (e.g. with
a spoken URL in them) are regenerated.
"""
import re

SPEECH_RULES_VERSION = 1

# Spoken when the first paragraph had words but nothing speakable survived
# (it was links / citations only).
LINK_FALLBACK = "I put the link on screen."
DETAIL_FALLBACK = "I put the details on your screen."

# ── pieces that vanish wholesale ─────────────────────────────────────────────
_FENCE = re.compile(r"(`{3,}|~{3,}).*?\1[^\n]*", re.S)          # fenced blocks
_FENCE_MARK = re.compile(r"(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*$")  # unclosed fence
_INLINE_CODE = re.compile(r"(?<!`)`[^`\n]+`")                   # `x` -> gone
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")                 # ![alt](url) -> gone
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")                 # [label](url) -> label
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)              # bare urls
_ANGLE_URL = re.compile(r"<\s*(?:https?://|www\.|mailto:)[^>]*>", re.I)
# [1] [^1] (1) [2, 3] [1-3] [source: x] (citation needed) — numeric refs and
# bracketed attributions, not ordinary parenthesised prose like "(the big one)".
_CITE = re.compile(
    r"""[\[(]\s*(?:\^?\d+(?:\s*[,\-–—]\s*\^?\d+)*   # [1] [2, 3] [1-3]
              |(?:note|source|citation|ref)s?\b[^)\]]*)\s*[\])]""",
    re.I | re.X)

# ── markdown furniture (markers only; the words stay) ───────────────────────
_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+")
_QUOTE = re.compile(r"(?m)^[ \t]{0,3}>[ \t]?")
_BULLET = re.compile(r"(?m)^[ \t]*[-*+\u2022\u00b7][ \t]+(?=\S)")
_RULE = re.compile(r"(?m)^[ \t]*(?:[-*_][ \t]*){3,}$")
_BOLD = re.compile(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1", re.S)   # **x** __x__ ~~x~~ -> x
_STAR = re.compile(r"\*{1,3}")                                  # any leftover asterisks
_UNDER = re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")  # _x_ -> x
# Pictographs, emoticons, dingbats, flags, keycaps, plus the joiners they use.
# Deliberately NOT in range: em/en dashes, arrows-only rows we keep, CJK.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF"      # mahjong..symbols extended (incl. emoticons)
    "\U0001F1E6-\U0001F1FF"       # regional indicators (flags; inside range above)
    "\u2600-\u27BF"               # misc symbols + dingbats
    "\u2B00-\u2BFF"               # arrows/star of david etc. (⬆ ⭐ ...)
    "\u2B50\u2B55\uFE0F\u20E3\u2122\u2139"
    "\u2049\u203C\u24C2\u2934\u2935\u3030\u303D\u3297\u3299]+")

_HAS_WORD = re.compile(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF\u3040-\u30FF\u0590-\u05FF\u0600-\u06FF]")
_EMPTY_PAIR = re.compile(r"[\[(]\s*[\])]")
_SENT_PUNCT = ".,;:!?'\""


def _trim_url(match) -> str:
    """A bare URL eats every non-space after it, but sentence punctuation at
    its tail is prose ('...gif, cute!') — put that punctuation back."""
    u, kept = match.group(0), ""
    while u:
        ch = u[-1]
        if ch in _SENT_PUNCT or (ch == ")" and "(" not in u):
            kept, u = ch + kept, u[:-1]
        else:
            break
    return kept


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_body(reply: str) -> str:
    """All removal rules over the whole reply (paragraph split comes after,
    so a URL-only paragraph still 'counts' as the first paragraph)."""
    text = str(reply or "")
    text = _FENCE.sub(" ", text)
    text = _FENCE_MARK.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _ANGLE_URL.sub(" ", text)
    text = _URL.sub(_trim_url, text)
    text = _CITE.sub(" ", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _RULE.sub("", text)
    text = _BOLD.sub(lambda m: m.group(2), text)
    text = _UNDER.sub(lambda m: m.group(1), text)
    text = _STAR.sub("", text)
    text = _EMOJI.sub(" ", text)
    text = _EMPTY_PAIR.sub(" ", text)
    # tidy seams left by removals: "words , more" / "words . more" / " ,"
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"(?m)^[,.;:!?][ \t]+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def contains_url(text: str) -> bool:
    return bool(_URL.search(str(text or "")) or _ANGLE_URL.search(str(text or "")))


def speech_text(reply: str) -> str:
    """The words to hand TTS for this reply. '' only when the reply itself
    had no words at all; a worded reply never returns ''."""
    text = str(reply or "")
    if not text.strip():
        return ""
    cleaned = clean_body(text)
    first = re.split(r"\n[ \t]*\n", cleaned, maxsplit=1)[0]
    spoken = _collapse(first)
    if spoken:
        return spoken
    # first paragraph survived as nothing speakable — say where the rest is
    if _HAS_WORD.search(text):
        return LINK_FALLBACK if contains_url(text) else DETAIL_FALLBACK
    return ""
