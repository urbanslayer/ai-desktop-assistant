"""Key-combo parsing + the forbidden-combo gate (docs/TYPING.md §5).

`press_keys("ctrl+s")`, `press_keys("ctrl+shift+t ctrl+pageup")`,
`backspace*3`. Super/Meta-as-Super/hyper never pass; ctrl+alt+Del,
ctrl+alt+Backspace and ctrl+alt+F1..F12 never pass — those belong to the
compositor. A denied combo is refused BEFORE any keystroke goes out."""
from .keycodes import (BASE_CHARS, FORBIDDEN_COMBOS, FORBIDDEN_CTRL_ALT_F,
                       FORBIDDEN_MODIFIERS, MODIFIERS, NAMED_KEYS, SUPER_CODE)
from ..client_tools import ToolError

ENTER_CODES = {28, 96}   # KEY_ENTER + KP_ENTER — the "run it" keys

# docs/TYPING.md §6: everything that acts like Enter *inside a terminal*.
# Keypad Enter is Enter; Ctrl+M is CR, Ctrl+J is LF; many terminals also
# pass Shift+Enter / Alt+Enter / Ctrl+Enter straight through as an
# Enter/newline. All of these execute the command line, so the terminal
# gate treats them exactly like plain Enter.
_TERMINAL_RUN_KEYS = {
    (frozenset(), 28), (frozenset(), 96),           # Enter / KP_Enter
    (frozenset({"ctrl"}), 50),                      # Ctrl+M  = CR
    (frozenset({"ctrl"}), 36),                      # Ctrl+J  = LF
    (frozenset({"shift"}), 28),                     # Shift+Enter (many terms)
    (frozenset({"alt"}), 28),                       # Alt+Enter
    (frozenset({"ctrl"}), 28),                      # Ctrl+Enter
}
# Ctrl+C/D/Z/\ interrupt or kill whatever the shell is running (§6): they
# get their own approval with the target window shown.
_TERMINAL_INTERRUPT_KEYS = {
    (frozenset({"ctrl"}), 46),                      # Ctrl+C
    (frozenset({"ctrl"}), 32),                      # Ctrl+D
    (frozenset({"ctrl"}), 44),                      # Ctrl+Z
    (frozenset({"ctrl"}), 43),                      # Ctrl+\
}


def terminal_key_kind(mods, base, code):
    """'run' | 'interrupt' | None for one parsed combo (see the tables)."""
    key = (frozenset(mods), code)
    if key in _TERMINAL_RUN_KEYS:
        return "run"
    if key in _TERMINAL_INTERRUPT_KEYS:
        return "interrupt"
    return None


def parse_combo(text):
    """'ctrl+shift+s' -> (mods, base_name, keycode). ToolError on anything
    forbidden or unknown. `mods` is a sorted tuple of modifier names."""
    raw = str(text or "").strip().lower()
    if not raw:
        raise ToolError("empty key combo")
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    if not parts:
        raise ToolError(f"unusable key combo: {text!r}")
    mods, base, count = [], None, 1
    for i, part in enumerate(parts):
        name, star, n = part.partition("*")
        name = name.strip()
        if i < len(parts) - 1:
            if name in FORBIDDEN_MODIFIERS:
                raise ToolError(
                    f"'{name}' is forbidden (docs/TYPING.md §5): Super belongs "
                    "to the compositor — Riko must not jump workspaces or log "
                    "her out")
            if name not in MODIFIERS:
                raise ToolError(f"unknown modifier in {text!r}: {name!r}")
            mods.append(name)
        else:
            if star and n.isdigit():
                count = max(1, min(64, int(n)))
            base = name
    if base is None:
        raise ToolError(f"key combo {text!r} has modifiers but no key")
    if base in FORBIDDEN_MODIFIERS or base == "super":
        raise ToolError(f"'{base}' alone is forbidden: it opens the "
                        "compositor overview (§5)")
    code = MODIFIERS.get(base, NAMED_KEYS.get(base))
    if code is None and len(base) == 1 and base in BASE_CHARS:
        code = BASE_CHARS[base][0]        # 'ctrl+s': the s keycode is layout-stable
    if code is None:
        # a lone printable char goes through the layout table upstream;
        # here only named keys are valid bases
        raise ToolError(f"unknown key in {text!r}: {base!r} (use a named key "
                        "like 'ctrl+s', 'enter' or 'backspace')")
    mods = tuple(dict.fromkeys(m for m in mods if m not in (base,)))
    modset = frozenset(mods) | {base}
    if modset in FORBIDDEN_COMBOS:
        raise ToolError(f"{'–'.join(['ctrl','alt',base])}: {base} with "
                        "ctrl+alt is compositor-owned (§5): nothing global, "
                        "no logout")
    if "ctrl" in mods and "alt" in mods and base in FORBIDDEN_CTRL_ALT_F:
        raise ToolError(f"ctrl+alt+{base} switches virtual terminals — "
                        "compositor-owned (§5)")
    if base in MODIFIERS and not mods:
        raise ToolError(f"{text!r} is just a modifier — hold it with a key")
    return mods, base, code, count


def parse_keys(text):
    """'ctrl+s enter' -> [combo, combo]. Combos split on whitespace/commas;
    '+' glues one combo together."""
    import re
    parts = [p for p in re.split(r"[,\s]+", str(text or "").strip()) if p]
    if not parts:
        raise ToolError("press_keys needs at least one key combo")
    if len(parts) > 32:
        raise ToolError("press_keys takes at most 32 combos per call")
    return [parse_combo(p) for p in parts]


def combo_events(mods, code):
    """[(keycode, down), …]: modifiers held, key tapped, modifiers released."""
    events = []
    for m in mods:
        events.append((MODIFIERS[m], True))
    events.append((code, True))
    events.append((code, False))
    for m in reversed(mods):
        events.append((MODIFIERS[m], False))
    return events


def is_plain_enter(mods, base):
    return not mods and base in ("enter", "return")
