"""Client-side mirror of hub/speech.py: the same spoken-text rules.

riko/tts.py cleans text before THIS PC's TTS (openai / sovits / fish) so she
sounds the same as with the hub's voice: in-sentence URLs, website names and
emails become short site names ("Blender", "the Godot Engine docs", "their
email address"), whole-line links and everything after the first blank line
stay dropped, no markdown. The hub deploys standalone (its own Docker image),
so this is a copy, not an import — tests/data/speech_cases.json pins both
halves to the same behaviour and a parity test fails if they drift.

If you edit these rules: edit hub/speech.py the same way AND bump
SPEECH_RULES_VERSION there (the hub's audio cache keys on it).
"""
import re

SPEECH_RULES_VERSION = 4

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
# Bare website names she must not dictate either: "docs.godotengine.org",
# api.example.io/v1, eve.local:8080. A known TLD always counts; an unknown
# letter TLD only when the token has >=2 dots or a path/port/query follows
# it — that is what keeps "config.yaml", "main.py" and "photo.jpg" speakable.
_TLDS = ("com|org|net|io|dev|app|ai|co|uk|de|jp|gg|tv|me|info|xyz|edu|gov|"
         "us|ca|au|fr|eu|ru|cn|nl|se|no|fi|ch|at|be|es|it|pl|br|in|kr")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_DOMAIN = re.compile(
    r"""(?<![\w.@/-])                          # not glued to a bigger token
    (?P<host>
        (?:%s\.)+(?:%s)                        # known TLD: example.com, bbc.co.uk
      | (?:%s\.){2,}[A-Za-z]{2,24}             # >=2 dots: any 2-24 letter TLD
      | (?:%s\.)+[A-Za-z]{2,24}(?=[:/??#])     # unknown TLD + path/port/query
    )
    (?![\w-])                                  # not "example.company"
    (?::\d{2,5})?(?:[/?#]\S*)?                 # optional port + path/query
    """ % (_LABEL, _TLDS, _LABEL, _LABEL), re.I | re.X)
# Emails, with their optional mailto: prefix. The lookbehind only blocks
# glued tokens; prose never has a word glued in front of an address.
_EMAIL = re.compile(
    r"(?<![^\s(])(?:mailto:)?[\w.+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}(?![\w-])", re.I)
# A trailing ".<ext>" from this list is a file name, not a website.
_FILE_EXTS = {
    "yaml", "yml", "py", "txt", "md", "markdown", "rst", "json", "toml", "ini",
    "cfg", "conf", "env", "log", "csv", "tsv", "xml", "html", "htm", "css",
    "scss", "js", "ts", "jsx", "tsx", "vue", "rb", "php", "pl", "lua", "java",
    "kt", "swift", "c", "h", "cpp", "hpp", "cs", "rs", "go", "sh", "bash",
    "zsh", "bat", "ps1", "sql", "db", "sqlite", "bak", "tmp", "swp", "lock",
    "zip", "tar", "gz", "bz2", "xz", "rar", "7z", "iso", "img", "dmg", "exe",
    "dll", "so", "deb", "rpm", "apk", "whl", "jar", "class", "o", "a", "lib",
    "pdf", "epub", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "rtf",
    "tex", "bib", "ttf", "otf", "woff", "woff2", "png", "jpg", "jpeg", "gif",
    "bmp", "tif", "tiff", "svg", "ico", "webp", "heic", "psd", "xcf", "eps",
    "blend", "fbx", "glb", "gltf", "wav", "flac", "mp3", "ogg", "opus", "m4a",
    "mp4", "mkv", "avi", "mov", "webm",
}
# Product names that END in a TLD-ish suffix but are never website mentions.
_TECH_KEEP = {
    "node.js", "vue.js", "nuxt.js", "next.js", "express.js", "react.js",
    "svelte.js", "three.js", "deno.js", "backbone.js", "ember.js",
    "socket.io", "asp.net",
}
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
_DASH = "\u2013\u2014"  # en/em dashes only; a plain hyphen is too common in prose

# ── speakable site names ─────────────────────────────────────────────────────
# An email inside a sentence is never dictated letter by letter.
EMAIL_PHRASE = "their email address"
# Well-known brands keyed by registered domain (host minus subdomains).
_SITE_NAMES = {
    "github.com": "GitHub",
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "wikipedia.org": "Wikipedia",
    "reddit.com": "Reddit",
    "stackoverflow.com": "Stack Overflow",
    "google.com": "Google",
    "amazon.com": "Amazon", "amazon.co.uk": "Amazon",
    "godotengine.org": "Godot Engine",
    "python.org": "Python",
    "blender.org": "Blender",
    "mozilla.org": "Mozilla",
    "x.com": "X", "twitter.com": "X",
}
# Brands whose name is the main label whatever the TLD is (amazon.de, ...).
_SITE_LABELS = {"amazon": "Amazon"}
# Subdomains say WHICH part of the site: docs.blender.org -> "the Blender docs".
_SITE_HINTS = {"docs": "docs", "doc": "docs", "wiki": "wiki",
               "forum": "forum", "forums": "forum",
               "store": "store", "shop": "shop", "blog": "blog"}
# Second-level TLDs: bbc.co.uk has its main label one deeper (bbc).
_CC_SECOND = {"co", "com", "net", "org", "gov", "ac", "edu", "or", "ne", "in"}
# Compound labels split on a known tail word: godotengine -> "Godot Engine",
# archlinux -> "Arch Linux" (longest tails first).
_SITE_WORDS = ("engine", "labs", "lab", "wiki", "docs", "doc", "apps", "app",
               "tools", "tool", "linux", "space", "base", "zone", "world",
               "works", "cloud", "news", "mag", "tube", "book")
# Short brands title() would mangle into "Bbc".
_SITE_ACRONYMS = {"bbc", "cnn", "npr", "ibm", "nasa", "espn", "fbi", "cia",
                  "mit", "nhs", "usa", "uk"}
# Sentence ends: words behind one belong to another sentence, so a domain
# after "Found one!" is a dangling link, not a sentence constituent.
_SENT_END = ".!?\u2026"
# Leading characters that never make text after a link "continue the sentence".
_STRIP_FRONT = ")]}\\\"'\u201c\u201d\u2019\u2026\u2014\u2013,;:- \t"


def _split_tail(u: str):
    """A bare URL eats every non-space after it, but sentence punctuation at
    its tail is prose ('...gif, cute!') — split it off as (url, kept)."""
    kept = ""
    while u:
        ch = u[-1]
        if ch in _SENT_PUNCT or (ch == ")" and "(" not in u):
            kept, u = ch + kept, u[:-1]
        else:
            break
    return u, kept


def _trim_url(match) -> str:
    """Kept sentence punctuation of a dropped URL-ish match."""
    return _split_tail(match.group(0))[1]


def _prettify_label(label: str) -> str:
    """'godotengine' -> 'Godot Engine'; 'bbc' -> 'BBC'; unsure -> title-cased."""
    word = label.lower().replace("-", "").replace("_", "")
    if not word:
        return "that website"
    if word in _SITE_ACRONYMS:
        return word.upper()
    for tail in _SITE_WORDS:
        if word.endswith(tail) and len(word) > len(tail):
            return word[: -len(tail)].title() + " " + tail.title()
    return word.title()


def _site_name(host: str) -> str:
    """Short speakable name for a host: 'blender.org/download's host
    'blender.org' -> 'Blender'; 'docs.godotengine.org' -> 'the Godot Engine
    docs'; unknown single-letter labels -> 'that website'."""
    labels = [p for p in str(host).lower().split(".") if p]
    if len(labels) > 2 and labels[0] == "www":
        labels = labels[1:]
    if len(labels) < 2:
        return labels[0].title() if labels else "that website"
    if len(labels) >= 3 and labels[-2] in _CC_SECOND:
        main, root = labels[-3], ".".join(labels[-3:])
    else:
        main, root = labels[-2], ".".join(labels[-2:])
    name = _SITE_NAMES.get(root) or _SITE_LABELS.get(main)
    if name is None:
        name = _prettify_label(main) if len(main) > 1 else "that website"
    hint = _SITE_HINTS.get(labels[0])
    if hint and len(labels) >= 3 and name != "that website":
        name = "the %s %s" % (name, hint)
    return name


def _in_sentence(match) -> bool:
    """True when the link-shaped match sits INSIDE a sentence — other words
    of the same sentence before it, or the sentence continuing after it.
    A link that is the whole line, or dangles after a sentence end
    ("Found one! https://..."), is not a constituent and stays dropped."""
    s = match.string
    line_start = s.rfind("\n", 0, match.start()) + 1
    line_end = s.find("\n", match.end())
    line_end = len(s) if line_end < 0 else line_end
    before = s[line_start:match.start()]
    after = s[match.end():line_end]
    if not (_HAS_WORD.search(before) or _HAS_WORD.search(after)):
        return False
    same_sentence = re.split("[%s]" % _SENT_END, before)[-1]
    return bool(_HAS_WORD.search(same_sentence)
                or _HAS_WORD.search(after.lstrip(_STRIP_FRONT)))


def _domain_survives(match) -> bool:
    """True for matches that only LOOK like domains: tech product names
    (Node.js, ASP.NET) and file names (main.py, photo.jpg, main.py:8000)."""
    host = match.group("host").lower()
    return (host in _TECH_KEEP
            or host.rsplit(".", 1)[-1] in _FILE_EXTS)


def _name_url(match) -> str:
    core, kept = _split_tail(match.group(0))
    if not _in_sentence(match):
        return kept
    host = re.split(r"[/?#:]", re.sub(r"^(?:https?://|www\.)", "", core,
                                      flags=re.I), maxsplit=1)[0]
    return _site_name(host) + kept


def _name_angle(match) -> str:
    inner = re.sub(r"^(?:https?://|www\.|mailto:)", "",
                   match.group(0).strip("<> \t"), flags=re.I)
    if not _in_sentence(match):
        return " "
    if "@" in inner:
        return EMAIL_PHRASE
    return _site_name(re.split(r"[/?#:]", inner, maxsplit=1)[0])


def _name_email(match) -> str:
    return EMAIL_PHRASE if _in_sentence(match) else " "


def _drop_domain(match) -> str:
    if _domain_survives(match):
        return match.group(0)
    kept = _trim_url(match)  # keeps sentence punctuation at the tail of a path
    if not _in_sentence(match):
        return kept
    return _site_name(match.group("host")) + kept


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_body(reply: str) -> str:
    """All removal/replacement rules over the whole reply (paragraph split
    comes after, so a URL-only paragraph still 'counts' as the first
    paragraph)."""
    text = str(reply or "")
    text = _FENCE.sub(" ", text)
    text = _FENCE_MARK.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text, n_angle = _ANGLE_URL.subn(_name_angle, text)
    text, n_url = _URL.subn(_name_url, text)
    text, n_mail = _EMAIL.subn(_name_email, text)
    text, n_dom = _DOMAIN.subn(_drop_domain, text)
    # did a link-shaped removal strand a preposition or dash on the seam?
    left_gaps = bool(n_angle or n_url or n_mail or n_dom)
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
    # tidy seams left by removals: "words , more" / "words . more" / " ,".
    # Only where the punctuation really ends a clause (space/punct/EOL after
    # it): otherwise "the .NET runtime" loses the space before a file-name-
    # style token like ".NET". Stray spaces inside brackets/quotes ("(x )")
    # go wherever they show up — named replacements leave those too.
    text = re.sub(r"[ \t]+([,.;:!?])(?=[\s,.;:!?\u2013\u2014]|\Z)", r"\1", text)
    text = re.sub(r"(?m)^[,.;:!?][ \t]+", "", text)
    text = re.sub(r"([\[(])[ \t]+", r"\1", text)
    text = re.sub(r"[ \t]+([\])])", r"\1", text)
    if left_gaps:
        # "senpai —, the" -> "senpai — the"; a dash stranded before sentence
        # punctuation / EOL, doubled dashes, and an 'at/on/from' left
        # pointing at nothing ("Hosted at", "runs on.") must not be spoken.
        text = re.sub(r"[%s]\s*,\s*" % _DASH, lambda m: m.group(0)[0] + " ", text)
        text = re.sub(r"(?m)[ \t]+[%s]+[ \t]*(?=[,.;:!?])" % _DASH, "", text)
        text = re.sub(r"(?m)[ \t]+[%s]+[ \t]*$" % _DASH, ".", text)
        text = re.sub(r"([ \t][%s][ \t]*){2,}" % _DASH, " \u2014 ", text)
        # a weak separator stranded right where the link was must yield to
        # the clause end it now touches: "gave you, docs.x ." became
        # "gave you,." and has to be spoken "gave you." (same for ";." ":."
        # ".," and doubled commas); empty quotes/parens left by the removal
        # ("", "(see )") go too.
        text = re.sub(r"[,;:][ \t]*([.!?])", r"\1", text)
        text = re.sub(r"([.!?])[ \t]*[,;:]+", r"\1", text)
        text = re.sub(r",[ \t]*,", ",", text)
        text = re.sub(r'["\u201c\u201d][ \t]*["\u201c\u201d]', "", text)
        text = re.sub(r"[\[(][ \t]*(?:[^\s()\[\]]+[ \t]+){1,2}[\])][ \t]*",
                      " ", text)
        text = re.sub(r"[ \t]+\b(?:at|on|from)\b[ \t]*(?=[,.;:!?\u2013\u2014]|\Z)",
                      "", text, flags=re.I)
        # a line that now ENDS on a stranded separator gets a real ending:
        # "the link is," -> "the link is.", "link:" -> "link."
        text = re.sub(r"(?m)[ \t]*[,;:]+[ \t]*$", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def contains_url(text: str) -> bool:
    """Anything the link-shaped fallback covers: URLs, emails, and bare
    domains the cleaner would actually remove (files/tech names don't count)."""
    t = str(text or "")
    if _URL.search(t) or _ANGLE_URL.search(t) or _EMAIL.search(t):
        return True
    return any(not _domain_survives(m) for m in _DOMAIN.finditer(t))


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
