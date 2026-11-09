"""Linux evdev keycodes (input-event-codes.h) — ydotool speaks these.

The layout table (layout.py) turns characters into (keycode, modifiers);
this module holds the layout-independent part: named keys and modifiers.
`super`/`meta`/`hyper` parse fine but are FORBIDDEN combos (docs/TYPING.md
§5) — the compositor owns the Super key, so keys.py rejects them before
they ever become keycodes.
"""

# ── modifiers ─────────────────────────────────────────────────
MODIFIERS = {
    "ctrl": 29, "control": 29, "ctrl_l": 29, "ctrlleft": 29,
    "ctrl_r": 97, "ctrlright": 97,
    "shift": 42, "shift_l": 42, "shiftleft": 42,
    "shift_r": 54, "shiftright": 54,
    "alt": 56, "alt_l": 56, "meta": 56,   # Alt is Meta to the window manager
    "alt_gr": 100, "altgr": 100, "meta_r": 100,
}
# modifiers the owner's compositor owns: ctrl+alt+Del and super+anything
# never come from Riko (§5: nothing global, no workspace jumps, no logout)
FORBIDDEN_MODIFIERS = {"super", "super_l", "super_r", "win", "cmd", "hyper"}
SUPER_CODE = 125   # KEY_LEFTMETA: parsed for error messages, never sent (§5)

# ── named keys (layout-independent) ───────────────────────────
NAMED_KEYS = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10,
    "0": 11,
    "minus": 12, "hyphen": 12, "equal": 13, "equals": 13,
    "backspace": 14,
    "tab": 15,
    "enter": 28, "return": 28, "kp_enter": 96, "keypadenter": 96,
    "backslash": 43,
    "capslock": 58,
    "space": 57,
    "pgup": 104, "pageup": 104, "pgdn": 109, "pagedown": 109,
    "home": 102, "end": 107,
    "left": 105, "up": 103, "down": 108, "right": 106,
    "insert": 110, "delete": 111, "del": 111,
    "print": 210, "printscreen": 210,
    "volumeup": 115, "volumedown": 114, "mute": 113,
}
for _i, _name in enumerate(("f1", "f2", "f3", "f4", "f5", "f6",
                            "f7", "f8", "f9", "f10", "f11", "f12")):
    NAMED_KEYS[_name] = 59 + _i   # F1-F10 = 59-68, F11 = 87, F12 = 88
NAMED_KEYS["f11"], NAMED_KEYS["f12"] = 87, 88

# printable ASCII that lives on the same key on EVERY supported layout;
# the layout table may override these (it only needs the exceptions).
# evdev letter codes are NOT alphabetical — this is the physical QWERTY order.
_LETTER_CODES = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25,
     16, 19, 31, 20, 22, 47, 17, 45, 21, 44)))
BASE_CHARS = {c: (code,) for c, code in _LETTER_CODES.items()}
BASE_CHARS.update({c: (code,) for c, code in
                   dict(zip("1234567890",
                            (2, 3, 4, 5, 6, 7, 8, 9, 10, 11))).items()})
BASE_CHARS.update({
    " ": (57,), "-": (12,), "=": (13,), "[": (26,), "]": (27,),
    ";": (39,), "'": (40,), "\\": (43,), ",": (51,), ".": (52,), "/": (53,),
    "`": (41,),
})

# ── combos that must never come from Riko (§5) ────────────────
FORBIDDEN_COMBOS = {
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "alt", "backspace"}),
}
# ctrl+alt+F1..F12 = VT switches (compositor-owned)
FORBIDDEN_CTRL_ALT_F = {f"f{i}" for i in range(1, 13)}
