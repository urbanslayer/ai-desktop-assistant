"""Terminal identification (docs/TYPING.md §6): the app classes Riko may
type into must be treated as shells. In a terminal Riko never sends Enter
without the owner approving the EXACT command, newlines inside typed text
are stripped, and paste uses Ctrl+Shift+V."""

TERMINAL_CLASSES = {
    "kitty", "foot", "footclient", "alacritty", "wezterm", "konsole",
    "org.kde.konsole", "gnome-terminal-server", "org.gnome.Terminal",
    "kgx", "org.gnome.Ptyxis", "ptyxis", "xterm", "uxterm", "XTerm",
    "ghostty", "tilix", "com.gexperts.Tilix", "terminator", "terminology",
    "st", "simpleterm", "xfce4-terminal", "lxterminal", "deepin-terminal",
    "rio", "vitest-terminal",
}
# generic fallbacks matched as substrings (class or title)
_TERMINAL_HINTS = ("terminal", "konsole", "alacritty", "wezfurlong")


def is_terminal(app: str = "", title: str = "") -> bool:
    """True when the focused window is a terminal (shell risk applies).

    Matched on the window class first (exact or dot-segment), then on
    unmistakable substrings of the class/title so `user@arch: ~` style
    titles never guess wrong on their own."""
    cls = (app or "").strip()
    low_cls = cls.lower()
    if low_cls in {c.lower() for c in TERMINAL_CLASSES}:
        return True
    for seg in low_cls.replace("/", ".").split("."):
        if seg in {c.lower() for c in TERMINAL_CLASSES} and seg not in ("org", "com", "io"):
            return True
    hay = low_cls + " " + (title or "").lower()
    return any(h in low_cls for h in _TERMINAL_HINTS)
