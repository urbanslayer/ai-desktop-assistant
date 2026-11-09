"""Layout-aware char → evdev keycode mapping (docs/TYPING.md §4).

The owner is on `gb`; the model thinks in characters. A hardcoded US table
would type `@` for what she sees as `"`. The active layout is detected from
the DesktopSense backend (Hyprland: `hyprctl -j devices` main keyboard's
active_keymap, falling back to `getoption input:kb_layout` and the Lua /
classic config via the hyprlua reader; KDE: kxkbrc; GNOME: gsettings),
default `us`. Unsupported layouts degrade to `us` with a visible note —
never a silent wrong character.
"""
import re
from pathlib import Path

from .keycodes import BASE_CHARS

# ── tables ────────────────────────────────────────────────────
# shift characters: char -> base keycode (shift is applied by resolve_char)
_US_SHIFT = {
    "!": 2, "@": 3, "#": 4, "$": 5, "%": 6, "^": 7, "&": 8, "*": 9,
    "(": 10, ")": 11, "_": 12, "+": 13, "{": 26, "}": 27, ":": 39,
    '"': 40, "~": 41, "|": 43, "<": 51, ">": 52, "?": 53,
}
_GB_SHIFT = {
    "!": 2, '"': 3, "£": 4, "$": 5, "%": 6, "^": 7, "&": 8, "*": 9,
    "(": 10, ")": 11, "_": 12, "+": 13, "{": 26, "}": 27, ":": 39,
    "@": 40, "¬": 41, "~": 43, "|": 94, "<": 51, ">": 52, "?": 53,
}


def _build(shift, unshift_overrides):
    # BASE_CHARS values are bare (keycode,) tuples; our tables are (keycode, mods)
    table = {c: (code, ()) for c, (code,) in BASE_CHARS.items()}
    table["\\"] = (43, ())     # US position; gb overrides below
    table.update({c: (code, ()) for c, code in unshift_overrides.items()})
    table.update({c: (code, ("shift",)) for c, code in shift.items()})
    return table


LAYOUTS = {
    "us": _build(_US_SHIFT, {}),
    # xkb `gb`: ' @ share the apostrophe key, # ~ sit beside P (evdev 43),
    # \ | live on the 102nd key left of Z (evdev 94), 2 " and 3 £ replace
    # the US " on the apostrophe and 4 $.
    "gb": _build(_GB_SHIFT, {"#": 43, "\\": 94}),
}
DEFAULT_LAYOUT = "us"

_ALIASES = {
    "gb": "gb", "uk": "gb", "en_gb": "gb", "british": "gb",
    "british english": "gb", "english (uk)": "gb", "english (uk)": "gb",
    "english (british)": "gb", "mac gb": "gb",
    "us": "us", "en_us": "us", "american": "us", "english (us)": "us",
    "english (us)": "us", "us intl": "us", "english (us, intl.)": "us",
    "english (us international)": "us", "mac us": "us",
}


def normalize_layout(name: str):
    """Map a keymap description ('British English', 'English (US)', 'gb')
    onto a table we own; None when we have no table for it."""
    if not name:
        return None
    s = str(name).strip().lower().replace("_", "-")
    for probe in (s, s.replace("-", "_"), s.split("(")[0].strip()):
        for key, table in _ALIASES.items():
            if probe == key.replace("-", "_") or probe == key:
                return table
    # 'English (US)' arrives with the variant in parens: match the parens
    m = re.search(r"\(([^)]+)\)", s)
    if m:
        inner = m.group(1).strip()
        if inner in ("us", "uk", "gb"):
            return _ALIASES.get(inner)
    return None


# ── resolve ───────────────────────────────────────────────────
def resolve_char(layout: str, ch: str):
    """(keycode, modifiers) for one character, or None when the layout
    cannot produce it (→ the caller falls back to clipboard paste)."""
    table = LAYOUTS.get(layout) or LAYOUTS[DEFAULT_LAYOUT]
    hit = table.get(ch)
    if hit is None and ch.isalpha() and ch.isupper():
        base = table.get(ch.lower())
        if base is not None:
            mods = tuple(dict.fromkeys(base[1] + ("shift",)))
            hit = (base[0], mods)
    if hit is None:
        return None
    code, mods = hit
    return code, list(mods)


# ── detection ─────────────────────────────────────────────────
_KB_LAYOUT_RE = re.compile(
    r"""kb_layout["']?\s*(?:=|,)\s*["']?([A-Za-z][A-Za-z0-9_()+,.-]*)""")


def detect_layout(backend_kind: str = "", runner=None, env=None,
                  home=None, config_dir=None, hypr_files=None, log=None):
    """Returns (layout, source_note). Order follows docs/TYPING.md §4;
    every external probe goes through `runner` (fake in tests) or a plain
    file read under `home` — never the owner's live session in tests."""
    env = env if env is not None else __import__("os").environ
    home = Path(home) if home else Path.home()
    note = []

    def say(msg):
        note.append(msg)
        if log:
            log(msg)

    def probe_layout(desc):
        lay = normalize_layout(desc)
        if lay:
            return lay
        if desc:
            say(f"[typing] layout '{desc}' has no keycode table; using "
                f"'{DEFAULT_LAYOUT}' — say so if the first characters look wrong")
        return None

    # 1. Hyprland, live first: main keyboard's active_keymap
    if backend_kind == "hyprland" and runner is not None:
        import json
        try:
            done = runner(["hyprctl", "-j", "devices"], timeout=3.0)
            if done.returncode == 0:
                for kb in json.loads(done.stdout).get("keyboards", []):
                    if kb.get("main"):
                        lay = probe_layout(kb.get("active_keymap")
                                           or kb.get("layout") or "")
                        if lay:
                            return lay, "hyprctl devices active_keymap"
        except Exception as exc:   # noqa: BLE001 — probe, never fatal
            say(f"[typing] hyprctl devices unreadable: {exc}")
        try:
            done = runner(["hyprctl", "-j", "getoption", "input:kb_layout"],
                          timeout=3.0)
            if done.returncode == 0:
                lay = probe_layout(json.loads(done.stdout).get("str", ""))
                if lay:
                    return lay, "hyprctl getoption input:kb_layout"
        except Exception:          # noqa: BLE001
            pass
        # 2. the config itself (Lua or classic): what she INTENDS
        for path in _hypr_config_files(home, config_dir, hypr_files):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _KB_LAYOUT_RE.search(text)
            if m:
                lay = probe_layout(m.group(1).split(",")[0])
                if lay:
                    return lay, f"{path.name} kb_layout"
    elif backend_kind != "hyprland":
        desk = str(env.get("XDG_CURRENT_DESKTOP", "")).lower()
        # 3. KDE: kxkbrc
        kxkb = home / ".config" / "kxkbrc"
        if ("kde" in desk or "plasma" in desk or backend_kind in ("kde", "plasma")
                or kxkb.is_file()):
            try:
                text = kxkb.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"^layout=(\S+)", text, re.M)
                if m:
                    lay = probe_layout(m.group(1).split(",")[0])
                    if lay:
                        return lay, "kxkbrc"
            except OSError:
                pass
        # 4. GNOME: gsettings input-sources
        if (("gnome" in desk or "ubuntu" in desk or backend_kind == "gnome")
                and runner is not None):
            try:
                done = runner(["gsettings", "get", "org.gnome.desktop.input-sources",
                               "sources"], timeout=3.0)
                if done.returncode == 0:
                    m = re.search(r"'xkb',\s*'([^']+)'", done.stdout)
                    if m:
                        lay = probe_layout(m.group(1))
                        if lay:
                            return lay, "gsettings input-sources"
            except Exception:      # noqa: BLE001
                pass

    return DEFAULT_LAYOUT, f"default ({'; '.join(note)})" if note else "default"


def _hypr_config_files(home: Path, config_dir, hypr_files):
    files = [Path(p) for p in (hypr_files or [])]
    if files:
        return files
    base = Path(config_dir) if config_dir else home / ".config" / "hypr"
    if not base.is_dir():
        return []
    out = []
    for pat in ("hyprland.lua", "hyprland.conf"):
        p = base / pat
        if p.is_file():
            out.append(p)
    for p in sorted(base.glob("*.lua")) + sorted(base.glob("*.conf")):
        if p not in out:
            out.append(p)
    return out
