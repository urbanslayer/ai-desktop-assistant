"""What she *says* vs what she *shows*: turn a stored reply into TTS text.

Her replies carry markdown, links and citations that look fine in a bubble
but sound awful read aloud ("https colon slash slash..."). The displayed
text keeps everything; the AUDIO gets this cleaned text instead:

  * fenced code blocks and inline code spans are dropped (nobody dictates code),
  * markdown images are dropped; markdown links keep only the label,
  * a bare URL, website name ("blender.org/download") or email that sits
    INSIDE a sentence is spoken as a short site name, never dictated:
    blender.org -> "Blender", docs.godotengine.org -> "the Godot Engine
    docs" (well-known names, a subdomain hint like docs./wiki./forum./
    store./shop./blog., a compound-label split, else title-cased label);
    an email in a sentence becomes "their email address". Version numbers
    (3.10.2), file names (config.yaml, main.py), decimals, abbreviations
    (e.g., U.S.) and tech names (Node.js, ASP.NET, Socket.IO) stay as-is,
  * URLs/domains that ARE the whole line, or dangle after a sentence end
    ("Found one! https://..."), are still dropped, as are bracketed/
    parenthetical source citations like [1], [2, 3] or (source: x), and
    markdown furniture: heading '#', '>' quotes, '-'/ '*' / '1.' list
    markers, emphasis '**' '*' '_' '~~', horizontal rules (the WORDS of
    prose lines all stay),
  * emoji and other pictographs are dropped (breeze reads their names),
  * per docs/TOOLS.md A EVERY prose paragraph is spoken, not just the first
    one — a multi-paragraph answer must not be cut after sentence two. What
    gets dropped instead are DETAILS blocks: paragraphs that, after removing
    links, are empty or only link labels/introductions ("Docs at X:",
    "link: X"), bullet or numbered lists of links, markdown tables, and
    "Sources:"/"Links:"-style sections (a label line starting such a section
    cuts its own paragraph too). Prose bullet lists ARE spoken (markers
    removed, each item a sentence). If nothing speakable survives at all
    (the reply was links/details only), a short fallback line is spoken —
    a reply with words never becomes empty audio,
  * the whole spoken text is capped at MAX_SPOKEN_CHARS, cut at a sentence
    end: Breeze splits long text itself (docs/server.md v0.1.6, see the
    hub/voice.py header), but a wall of prose is not a spoken answer anyway,
  * seams left by link-shaped REMOVALS are tidied: "gave you, x.org ." is
    spoken "gave you." (never ",." ";." ":." "— ." or a trailing ","/":"),
    and "( )", '""', a stranded dash or a dangling "at/on/from" go away.
    A domain that became a site name fills its own gap and needs no tidy-up.

`riko/speech.py` is the client-side mirror (the hub deploys standalone and the
client must clean identically for tts: openai/sovits/fish). The two files are
kept in lockstep by the shared table tests/data/speech_cases.json.

SPEECH_RULES_VERSION goes into every audio cache filename: bump it whenever
these rules change so cached clips synthesized under the old rules (e.g. with
a spoken URL in them) are regenerated.
"""
import re

SPEECH_RULES_VERSION = 7

# Upper bound on what one reply says out loud; text past it is cut at the
# last sentence end inside the cap (then at a word boundary). Documented in
# docs/API.md; Breeze itself has no input cap and splits long text on
# sentence boundaries server-side (hub/voice.py).
MAX_SPOKEN_CHARS = 2500

# Spoken when the reply had words but nothing speakable survived
# (it was links / citations / tables only).
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
_NUM_ITEM = re.compile(r"(?m)^[ \t]*\d{1,2}[.)][ \t]+(?=\S)")   # "1. x" -> "x"
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

# ── units read as words (weather round, docs/API.md "Weather") ─────────────
# "14 °C" must not be dictated "fourteen degree sign C": degree units and
# wind speeds are spoken as words. °C/°F KEEP their scale — dropping the C/F
# would silently turn a Fahrenheit reply into Celsius. A lone "14°" is just
# "degrees". km/h/mph expand so TTS never spells the abbreviations out.
_DEG_C = re.compile(r"[\u00b0\u2103]\s*[Cc](?![A-Za-z])")
_DEG_F = re.compile(r"[\u00b0\u2109]\s*[Ff](?![A-Za-z])")
_DEG_ANY = re.compile(r"\u00b0\s*(?![A-Za-z])")
_KMH = re.compile(r"(?<![\w/])k[m]\s*/\s*h(?![\w/])")
_MPH = re.compile(r"(?<![\w])mph(?![\w])")
# per-quantity units (unit-presets round): m/s and kn are weather units,
# never "m slash s" or "kay en". A bare "in" is inches only right after a
# number AND at a clause end — "5 in 10" / "3 in the night" keep the preposition.
_MS = re.compile(r"(?<![\w/])m\s*/\s*s(?![\w/])")
_KN = re.compile(r"(?<![\w])kn(?![\w])")
_INCH = re.compile(r"(?<=\d)(\s?)in\b(?=\s*(?:[.,;:!?)\]]|$))")

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

# ── details-block recognition (paragraphs that are shown, not spoken) ───────
# A section opener: when the rest of its paragraph is links-only, the label
# line cuts the paragraph ("Answer.\nSources:\n- https://a" speaks sentence 1).
_SECTION_LABEL = re.compile(
    r"(?i)^[ \t]{0,3}#{0,6}[ \t]*(?:sources?(?:[ \t]+and[ \t]+links?)?|links?"
    r"|references?|citations?|attributions?|details?|see[ \t]+also"
    r"|further[ \t]+reading|more[ \t]+(?:info(?:rmation)?|details?)"
    r"|for[ \t]+more(?:[ \t]+(?:info(?:rmation)?|details?))?)\s*:?\s*$")
# Words that introduce a link without being content ("Docs at X:", "link: X",
# "watch it: X"). Consulted ONLY for a line that actually carried a
# link-shaped token; a residue of at most _LINK_RESIDUE_WORDS of these words
# leaves nothing of substance behind, so the line was a link line. Real
# content words are never in here, and longer residues ("Link is X for you")
# are sentences ABOUT the link, so they keep the in-sentence naming rules.
_LINK_RESIDUE_WORDS = 3
_LINK_INTRO = {
    "a", "an", "the", "and", "or", "at", "on", "in", "to", "for", "from", "of",
    "here", "is", "are", "it", "its", "this", "that", "these", "those",
    "you", "your", "yours", "me", "my",
    "link", "links", "url", "urls", "docs", "doc", "documentation", "page",
    "pages", "site", "website", "address", "source", "sources", "reference",
    "references", "source", "video", "videos", "photo", "photos", "picture",
    "picture", "image", "images", "screenshot", "download", "downloads",
    "official", "see", "read", "watch", "check", "try", "open", "visit",
    "info", "information", "details", "detail", "via", "direct", "straight",
}
# Private-use sentinel wrapping markdown link LABELS in the details probe:
# a label is not prose, so it must not rescue a link list from the cut.
_L0 = "\ue000"
_CAP_SENT = re.compile(r"[.!?\u3002\uff01\uff1f\u2026]+(?=[\s\"'\u201c\u201d\u2019)\]]|$)")


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


def _line_linkish(line: str) -> bool:
    """True when a line is essentially JUST a link or a section opener:
    a 'Sources:'-style heading, or after images/URLs/emails/domains/citations
    are removed and list markers and markdown link LABELS are ignored, nothing
    is left but punctuation or link-introduction words ('Docs at', 'link:').
    Prose words ('hosted', 'cute', 'senpai') always rescue the line."""
    if _SECTION_LABEL.match(line):
        return True
    had_link = False
    l, n = _MD_IMAGE.subn(" ", line)
    had_link = n > 0
    l, n = _MD_LINK.subn(lambda m: _L0 + (m.group(1) or "") + _L0, l)
    had_link = had_link or n > 0
    for pat in (_ANGLE_URL, _URL, _EMAIL):
        l, n = pat.subn(" ", l)
        had_link = had_link or n > 0
    def _dom(m):
        nonlocal had_link
        if _domain_survives(m):
            return m.group(0)
        had_link = True
        return " "
    l = _DOMAIN.sub(_dom, l)
    if not had_link:
        return False
    l = _CITE.sub(" ", l)
    l = _HEADING.sub("", l)
    l = _QUOTE.sub("", l)
    l = _BULLET.sub("", l)
    l = _NUM_ITEM.sub("", l)
    l = re.sub("%s[^\uE000]*%s" % (_L0, _L0), " ", l)  # drop link labels
    l = re.sub(r"[^\w\s]", " ", l)
    words = [w for w in l.lower().split() if not w.isdigit()]
    return len(words) <= _LINK_RESIDUE_WORDS and all(w in _LINK_INTRO for w in words)


def _is_details_block(para: str) -> bool:
    """A paragraph that is on-screen material, not speech: empty, a markdown
    table, or lines that are all links / link intros / section labels.
    Prose lists ('- milk', '1. eggs') have content words and stay spoken."""
    lines = [ln for ln in para.splitlines() if ln.strip()]
    if not lines:
        return True
    pipes = sum(1 for ln in lines if "|" in ln)
    if len(lines) >= 2 and pipes >= 2 and pipes * 2 >= len(lines):
        return True  # markdown table
    return all(_line_linkish(ln) for ln in lines)


def _cut_section(para: str) -> str:
    """Cut a paragraph at a 'Sources:'-style label line when everything under
    the label is links ("Answer.\\nSources:\\n- https://a" -> "Answer.")."""
    lines = para.split("\n")
    for i, ln in enumerate(lines):
        if _SECTION_LABEL.match(ln):
            rest = [r for r in lines[i + 1:] if r.strip()]
            if all(_line_linkish(r) for r in rest):
                return "\n".join(lines[:i])
            break
    return para


def _strip_blocks(text: str) -> str:
    """Whole-block removals that must happen BEFORE the paragraph split:
    code fences (their content may hold blank lines) and markdown images."""
    text = _FENCE.sub(" ", text)
    text = _FENCE_MARK.sub(" ", text)
    text = _MD_IMAGE.sub(" ", text)
    return text


def clean_prose(text: str) -> str:
    """All inline removal/replacement rules over one block of text."""
    text = str(text or "")
    text = _DEG_F.sub(" degrees Fahrenheit", text)
    text = _DEG_C.sub(" degrees Celsius", text)
    text = _DEG_ANY.sub(" degrees", text)
    text = _KMH.sub(" kilometres an hour", text)
    text = _MPH.sub(" miles an hour", text)
    text = _MS.sub(" metres per second", text)
    text = _KN.sub(" knots", text)
    text = _INCH.sub(r"\g<1>inches", text)
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
    text = _NUM_ITEM.sub("", text)
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


def clean_body(reply: str) -> str:
    """All removal/replacement rules over the whole reply (the details-block
    split in speech_text comes after, so rule upgrades stay one code path)."""
    return clean_prose(_strip_blocks(str(reply or "")))


def _cap_spoken(text: str) -> str:
    """Speak at most MAX_SPOKEN_CHARS, cutting at the last sentence end
    inside the cap (word boundary if a lone sentence runs past it)."""
    if len(text) <= MAX_SPOKEN_CHARS:
        return text
    head = text[:MAX_SPOKEN_CHARS]
    ends = list(_CAP_SENT.finditer(head))
    if ends:
        return head[:ends[-1].end()].strip()
    cut = head.rfind(" ")
    return (head[:cut] if cut > MAX_SPOKEN_CHARS // 2 else head).rstrip()


def contains_url(text: str) -> bool:
    """Anything the link-shaped fallback covers: URLs, emails, and bare
    domains the cleaner would actually remove (files/tech names don't count)."""
    t = str(text or "")
    if _URL.search(t) or _ANGLE_URL.search(t) or _EMAIL.search(t):
        return True
    return any(not _domain_survives(m) for m in _DOMAIN.finditer(t))


def speech_text(reply: str) -> str:
    """The words to hand TTS for this reply. EVERY prose paragraph is spoken
    (docs/TOOLS.md A: links and details go after a blank line — they are the
    part dropped, not everything after it). '' only when the reply itself
    had no words at all; a worded reply never returns ''."""
    text = str(reply or "")
    if not text.strip():
        return ""
    blocks = [(p, _is_details_block(p))
              for p in re.split(r"\n[ \t]*\n", _strip_blocks(text)) if p.strip()]
    parts = []
    for i, (para, details) in enumerate(blocks):
        if details:
            continue
        spoken = _collapse(clean_prose(_cut_section(para)))
        if not spoken:
            continue
        # A paragraph ending on a dangling connector ("Here's how:") is a
        # lead-in — give it a real spoken ending when what it introduced was
        # dropped (code block, links) or nothing follows at all. When the
        # NEXT paragraph is spoken prose the colon is a true pause: keep it.
        follows = blocks[i + 1:]
        if spoken[-1] in ",;:" and (not follows or follows[0][1]):
            spoken = spoken[:-1] + "."
        parts.append(spoken)
    out = _cap_spoken(" ".join(parts))
    if out:
        return out
    # every paragraph survived as nothing speakable — say where the rest is
    if _HAS_WORD.search(text):
        return LINK_FALLBACK if contains_url(text) else DETAIL_FALLBACK
    return ""
