"""In-window keyboard shortcuts: defaults, parsing, validation.

The bindings live under ``desktop.shortcuts`` in character_config.yaml and are
served to the avatar page through GET /settings (riko/viewer.py). The page
matches them against key events while her window is focused, so they are
app-specific and never collide with the window manager.

Rules
  * Syntax: optional Ctrl/Alt/Shift modifiers + one key, e.g.
    "Ctrl+Shift+Backspace". Letters are case-insensitive; "Space" is the space
    bar; key names match KeyboardEvent.key (Enter, Escape, Backspace, Tab,
    F1..F12, "/"). " " also means Space.
  * NEVER Super/Meta/Win/Hyper (any spelling): that key belongs to the window
    manager. A binding that uses it, or an unknown modifier or key, is
    rejected at startup with a warning and ignored.
  * A value may be one binding or a list; the action fires on any of them.
    "" or [] disables the action; a missing action keeps its default.
  * One key bound to two actions is a mistake: the first action keeps the key
    and the clash is warned about.

resolve() is pure: it returns (normalized_map, warnings). The caller decides
whether to print the warnings (viewer.start does, once, at startup).
"""
import re

# action -> default binding(s). Normalized form: "Ctrl+Alt+Shift+Key", in that
# modifier order, letters uppercase. This is already normalized; the tests say so.
DEFAULTS = {
    "type": ("Enter", "/"),               # open the text box
    "talk": ("Space",),                   # hold to talk, release to send
    "persona_next": ("P",),               # next persona
    "hide": ("H",),                       # hide her (tray icon brings her back)
    "forget": ("Ctrl+Shift+Backspace",),  # forget the chat (press twice to confirm)
    "quit": ("Ctrl+Q",),
}

MODS = {"ctrl": "Ctrl", "control": "Ctrl", "ctl": "Ctrl",
        "alt": "Alt", "option": "Alt",
        "shift": "Shift"}
MOD_ORDER = ("Ctrl", "Alt", "Shift")

# Every spelling of the window manager's key we know of. Rejected, not mapped.
BANNED_MODS = {"super", "meta", "win", "windows", "hyper", "os", "cmd",
               "command", "gui", "mod4"}

KEY_ALIASES = {
    " ": "Space", "space": "Space",
    "enter": "Enter", "return": "Enter",
    "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete", "del": "Delete",
    "tab": "Tab", "insert": "Insert",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown",
    "arrowup": "ArrowUp", "up": "ArrowUp",
    "arrowdown": "ArrowDown", "down": "ArrowDown",
    "arrowleft": "ArrowLeft", "left": "ArrowLeft",
    "arrowright": "ArrowRight", "right": "ArrowRight",
}

_FKEY = re.compile(r"^f([1-9]|1\d|2[0-4])$")


def _key_name(part):
    """Canonical KeyboardEvent.key for one binding's key part; raises ValueError(why)."""
    if part == " ":
        return "Space"
    p = part.strip()
    low = p.lower()
    if low in KEY_ALIASES:
        return KEY_ALIASES[low]
    if _FKEY.match(low):
        return f"F{low[1:]}"
    if len(p) == 1 and p.isprintable():
        return p.upper() if p.isalpha() else p
    raise ValueError(f"unknown key {p!r} (use a letter, digit, /, Enter, Space, "
                     f"Escape, Backspace, Tab, F1..F12, ...)")


def normalize(binding):
    """Canonicalize one binding. Returns (canonical, None) or (None, why)."""
    raw = str(binding)
    if raw == " ":  # a lone space bar: strip() below would eat it
        return "Space", None
    if not raw.strip():
        return None, "empty key"
    parts = raw.split("+")
    key_part = parts.pop()  # the last token is the key; the rest are modifiers
    mods = set()
    for token in parts:
        low = token.strip().lower()
        if low in BANNED_MODS:
            return None, (f"{token.strip()!r} is the window manager's key — "
                          "shortcuts must stay off Super/Meta/Win; use Ctrl/Alt/Shift")
        if low not in MODS:
            return None, f"unknown modifier {token.strip()!r} (use Ctrl, Alt or Shift)"
        mods.add(MODS[low])
    try:
        key = _key_name(key_part)
    except ValueError as e:
        return None, str(e)
    return "+".join([m for m in MOD_ORDER if m in mods] + [key]), None


def resolve(raw=None, warn=True):
    """Merge user bindings over the defaults. Returns (map, warnings).

    map: action -> list of canonical bindings ([] = disabled). Unknown actions,
    bad bindings and duplicate keys are dropped with a warning; missing
    actions keep their default. warn=False returns the same warnings without
    printing them (viewer.start prints once at startup; callers that only need
    the map stay quiet).
    """
    warnings = []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        warnings.append(f"desktop.shortcuts: expected a mapping of action: key, "
                        f"got {type(raw).__name__}; using the defaults")
        raw = {}
    for action in raw:
        if action not in DEFAULTS:
            warnings.append(f"desktop.shortcuts: unknown action {action!r} "
                            f"(known: {', '.join(DEFAULTS)}); ignoring it")

    out, owners = {}, {}  # owners: canonical binding -> action that kept it
    for action, default in DEFAULTS.items():
        value = raw[action] if action in raw else list(default)
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        kept = []
        for item in items:
            if item is None or str(item) == "":  # "" anywhere disables the action
                kept = []
                break
            canonical, err = normalize(item)
            if err:
                warnings.append(f"desktop.shortcuts.{action}: ignoring {item!r}: {err}")
                continue
            owner = owners.get(canonical)
            if owner and owner != action:
                warnings.append(f"desktop.shortcuts.{action}: {canonical!r} is already "
                                f"bound to {owner}; keeping it there, ignoring it here")
                continue
            owners[canonical] = action
            kept.append(canonical)
        out[action] = kept

    if warn:
        for w in warnings:
            print(f"[shortcuts] {w}", flush=True)
    return out, warnings
