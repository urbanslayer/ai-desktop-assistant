"""The generic app layer: XDG .desktop index + the safe launcher (contract §5).

Rules from docs/DESKTOP_SENSE.md:
- only desktop ids / Exec lines from INSTALLED .desktop files ever run;
  the model never gets to hand us a shell string;
- field codes (%u %U %f %F %d %D %n %N %i %c %k %v %m) are stripped from
  Exec, quotes are unquoted per the desktop-entry spec, and the result runs
  as an argv list (no shell), so `foo; rm -rf /` in a Name/Exec is data;
- launches are detached (start_new_session, /dev/null stdio) and inherit
  the owner's environment;
- she never starts a compositor, window manager, login session or display
  manager, and never pulls a system power/session lever: entries whose
  Exec/TryExec binary or desktop id names one (Hyprland, sway, kwin*,
  startplasma*, gnome-session, sddm, Xorg, ...), entries that live in
  wayland-sessions/ or xsessions/, and systemctl/loginctl/shutdown/reboot/
  poweroff are hidden from desktop_apps and refused by open_app (owner rule
  after a nested-compositor crash killed their live session).
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path

from .api_errors import ToolError

_FIELD_CODE = re.compile(r"%[a-zA-Z]")


def xdg_app_dirs() -> list[Path]:
    """The applications/ dirs to index, user dir first (it overrides)."""
    home = Path.home()
    dirs = []
    if os.environ.get("XDG_DATA_HOME"):
        dirs.append(Path(os.environ["XDG_DATA_HOME"]) / "applications")
    else:
        dirs.append(home / ".local" / "share" / "applications")
    for part in (os.environ.get("XDG_DATA_DIRS") or
                 "/usr/local/share:/usr/share").split(":"):
        if part:
            dirs.append(Path(part) / "applications")
    return [d for d in dirs if d.is_dir()]


def parse_desktop_file(path: Path) -> dict | None:
    """The few Desktop Entry keys we need; None when not a launchable app."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = None
    kv: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            if section is not None:      # only the first [Desktop Entry]
                break
            section = line if line == "[Desktop Entry]" else None
            continue
        if section is None or "=" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        kv.setdefault(key.strip(), val.strip())
    if kv.get("Type", "Application").lower() != "application":
        return None
    if kv.get("Hidden", "").lower() == "true" or kv.get("NoDisplay", "").lower() == "true":
        return None
    name = kv.get("Name") or ""
    exec_line = kv.get("Exec") or ""
    if not name or not exec_line:
        return None
    return {
        "id": path.name,                       # org.kde.kate.desktop
        "app": path.stem,                      # org.kde.kate
        "name": _unquote_local(name),
        "generic": _unquote_local(kv.get("GenericName") or ""),
        "keywords": _unquote_local(kv.get("Keywords") or ""),
        "exec": exec_line,
        "tryexec": kv.get("TryExec") or "",
        "terminal": kv.get("Terminal", "").lower() == "true",
        "path": str(path),
        "categories": kv.get("Categories") or "",
        "startup_wmclass": kv.get("StartupWMClass") or "",
    }


def _unquote_local(value: str) -> str:
    """Drop the desktop locale-variant suffix noise and quoting."""
    return value.split("(", 1)[0].strip().strip('"')


def split_exec(exec_line: str) -> list[str]:
    """Exec line -> argv, with field codes stripped and quotes resolved.

    Implements the quoting rules of the spec (single quotes protect, double
    quotes protect and honour backslash escapes) WITHOUT touching shell
    metacharacters: ';', '$' and '&' survive as literal argv characters,
    which is exactly the injection guard (we never run a shell).
    """
    argv: list[str] = []
    token: list[str] = []
    i, n = 0, len(exec_line)
    started = False
    while i < n:
        ch = exec_line[i]
        if ch == "'":
            j = exec_line.find("'", i + 1)
            token.extend(exec_line[i + 1:j if j >= 0 else n])
            started = True
            i = (j if j >= 0 else n) + 1
            continue
        if ch == '"':
            i += 1
            while i < n and exec_line[i] != '"':
                if exec_line[i] == "\\" and i + 1 < n:
                    token.append(exec_line[i + 1])
                    i += 2
                else:
                    token.append(exec_line[i])
                    i += 1
            i += 1  # closing quote
            started = True
            continue
        if ch == "%" and i + 1 < n and _FIELD_CODE.match(exec_line[i:i + 2]):
            i += 2
            started = True
            continue
        if ch in " \t":
            if token or started:
                argv.append("".join(token))
                token, started = [], False
            i += 1
            continue
        token.append(ch)
        i += 1
    if token or started:
        argv.append("".join(token))
    return [a for a in argv if a]


# ── never start a compositor / session / power action ─────────
# Owner rule (docs/DESKTOP_SENSE.md): a child's nested-Hyprland experiment
# SIGSEGV'd their LIVE Hyprland. Neither the app nor the model may ever
# launch a second compositor, window manager, login session, display
# manager, or a system power/session lever. Matched against the Exec and
# TryExec binaries (also behind wrappers like `env`/`dbus-run-session`/
# `gtk-launch`), the desktop id/app name, and the .desktop file's path.
_SESSION_BINARIES = {
    # compositors / WMs
    "hyprland", "start-hyprland", "sway", "cage", "labwc", "niri", "river",
    "wayfire", "waymonad", "qtile", "awesome", "bspwm", "dwm",
    "herbstluftwm", "i3", "openbox", "xfwm4", "marco", "compiz",
    # shells / sessions
    "plasmashell", "startplasma-wayland", "startplasma-x11", "gnome-shell",
    "gnome-session", "gnome-session-binary", "mutter", "xfce4-session",
    "lxqt-session", "mate-session", "mate-session-binary",
    "cinnamon-session", "budgie-desktop", "ukui-session", "deepin-session",
    "lumina-session", "enlightenment",
    # display servers / servers
    "weston", "xorg", "xfree86", "xwayland", "xvfb", "xephyr", "xinit",
    "startx", "Xwayland",
    # display managers
    "sddm", "gdm", "gdm-x-session", "gdm-wayland-session", "lightdm",
    "lxdm", "light-locker", "lxdm-gtk", "sddm-helper",
}
# matched as prefixes (kwin_wayland/kwin_x11, startplasma*, gdm-wayland-…)
_SESSION_BIN_PREFIXES = ("kwin", "startplasma", "gnome-session", "gdm-",
                         "sddm-", "lightdm-", "weston", "hyprland", "xorg",
                         "xwayland", "xvfb", "xephyr", "xserver")
# bare desktop ids that name a session (wayland-sessions/gnome.desktop …)
_SESSION_IDS = _SESSION_BINARIES | {
    "gnome", "gnome-classic", "gnome-classic-wayland", "gnome-wayland",
    "plasma", "kde", "kde-plasma", "xfce", "xfce4", "lxqt", "lxde", "mate",
    "budgie", "pantheon", "ukui", "deepin", "cinnamon", "i3-wm", "x11",
    "wayland", "hydrland",
}
# system power/session levers: never something she fires off casually
_POWER_COMMANDS = {"systemctl", "loginctl", "shutdown", "reboot", "poweroff",
                   "halt"}
# Exec wrappers whose REAL payload is a later token (env FOO=1 Hyprland,
# dbus-run-session sway, gtk-launch hyprland, gio launch …/xsessions/x…)
_LAUNCHER_WRAPPERS = {"env", "sh", "bash", "dash", "zsh", "dbus-run-session",
                      "dbus-launch", "systemd-run", "gtk-launch", "gio",
                      "xdg-open", "systemctl"}
_SESSION_DIR_MARKERS = ("wayland-sessions/", "xsessions/")


def _tok_name(tok) -> str:
    """basename of a token, .desktop suffix dropped, lowercased."""
    return str(tok).rsplit("/", 1)[-1].removesuffix(".desktop").lower()


def _is_session_name(name: str) -> bool:
    return (name in _SESSION_BINARIES
            or any(name.startswith(p) for p in _SESSION_BIN_PREFIXES))


def block_reason(entry: dict) -> str | None:
    """Why this .desktop entry must never be launched, or None.
    'login-session' / 'compositor' / 'power' are the three families."""
    low = str(entry.get("path") or "").lower().replace("\\", "/")
    if any(m in low for m in _SESSION_DIR_MARKERS):
        return "login-session entry (wayland-sessions/xsessions)"
    for key in ("exec", "tryexec"):
        argv = split_exec(str(entry.get(key) or ""))
        if not argv:
            continue
        first = _tok_name(argv[0])
        tokens = argv[1:] if first in _LAUNCHER_WRAPPERS else argv[:1]
        if first in _POWER_COMMANDS:
            return f"system power/session command ({first})"
        if _is_session_name(first):
            return f"compositor/session binary ({first})"
        for tok in tokens:
            tl = str(tok).lower()
            if first in ("gtk-launch", "gio", "xdg-open", "systemd-run",
                         "dbus-run-session") \
                    and any(m in tl for m in _SESSION_DIR_MARKERS):
                return "login-session entry (wayland-sessions/xsessions)"
            tb = _tok_name(tok)
            if tb in _POWER_COMMANDS:
                return f"system power/session command ({tb})"
            if tb in _SESSION_IDS or _is_session_name(tb):
                return f"compositor/session binary ({tb})"
    for ident in (str(entry.get("app") or ""),
                  str(entry.get("id") or "").removesuffix(".desktop")):
        if ident.lower() in _SESSION_IDS:
            return f"compositor/session entry ({ident.lower()})"
    return None


class AppIndex:
    """Installed launchable apps, fuzzy-searched. Reads the XDG dirs lazily
    per call (apps get installed and uninstalled); `dirs` pins them (tests).

    Compositor / session / power entries (block_reason below) never appear
    in listings; `resolve` still sees them so open_app can REFUSE loudly
    instead of answering "no app matches".
    """

    def __init__(self, dirs=None):
        self._dirs = [Path(d) for d in dirs] if dirs else None

    def _search_dirs(self) -> list[Path]:
        return self._dirs or xdg_app_dirs()

    def all(self, include_blocked: bool = False) -> list[dict]:
        seen: dict[str, dict] = {}
        for d in self._search_dirs():
            try:
                entries = sorted(d.glob("*.desktop"))
            except OSError:
                continue
            for f in entries:
                app = parse_desktop_file(f)
                if app and app["id"] not in seen:   # user dir wins (first)
                    if include_blocked or block_reason(app) is None:
                        seen[app["id"]] = app
        return sorted(seen.values(), key=lambda a: a["name"].lower())

    def find(self, query: str = "", limit: int = 25) -> list[dict]:
        apps = self.all()
        q = str(query or "").strip().lower()
        if not q:
            return apps[:limit]
        qwords = [w for w in re.split(r"[^a-z0-9]+", q) if w]
        scored = []
        names = []
        for a in apps:
            hay = " ".join((a["app"], a["name"], a["generic"], a["keywords"])).lower()
            words = set(re.split(r"[^a-z0-9]+", hay))
            score = 0
            if q in (a["app"], a["id"].lower(), a["name"].lower()):
                score += 100
            if q in hay:
                score += 40
            for w in qwords:
                if w in words:
                    score += 20
                elif any(word.startswith(w) for word in words):
                    score += 8
                elif w in hay:
                    score += 4
            if score:
                scored.append((score, a))
            names.append(a["name"].lower())
        scored.sort(key=lambda sa: (-sa[0], sa[1]["name"].lower()))
        out = [a for _, a in scored]
        if len(out) < limit:   # near-misses ("crome" -> Chrome)
            for cand in difflib.get_close_matches(q, names, n=limit - len(out), cutoff=0.6):
                match = next((a for a in apps if a["name"].lower() == cand), None)
                if match and match not in out:
                    out.append(match)
        return out[:limit]

    def resolve(self, want: str) -> dict | None:
        """Exact desktop id / app id first, then best fuzzy match. None = no
        confident match (callers must NOT launch a weak guess). Blocked
        entries are matched here so open_app can refuse them by name."""
        want_l = str(want or "").strip().lower()
        want_l = re.sub(r"\.desktop$", "", want_l)
        if not want_l:
            return None
        apps = self.all(include_blocked=True)
        for a in apps:
            if want_l in (a["id"].lower(), a["app"].lower(),
                          a["name"].lower(), a["startup_wmclass"].lower()):
                return a
        for a in self.find(want_l, limit=5):
            hay_l = a["name"].lower()
            if want_l == hay_l or want_l in a["app"].lower().split() \
                    or hay_l.startswith(want_l) or want_l in hay_l.split():
                return a
        return None


# Loose category hints for "open my terminal / browser / files" when the
# compositor config (Hyprland Lua) says nothing.
_PREFERRED_HINTS = {
    "terminal": ("kitty", "foot", "alacritty", "wezterm", "ghostty",
                 "konsole", "org.kde.konsole", "xfce4-terminal",
                 "gnome-terminal", "org.gnome.terminal", "xterm", "st"),
    "browser": ("firefox", "firefox-esr", "org.mozilla.firefox", "chromium",
                "google-chrome", "brave-browser", "microsoft-edge",
                "vivaldi-stable", "org.gnome.epiphany"),
    "filemanager": ("thunar", "dolphin", "org.kde.dolphin", "nautilus",
                    "org.gnome.nautilus", "nemo", "pcmanfm-qt", "pcmanfm"),
}
_ALIAS = {"terminal": "terminal", "files": "filemanager", "file manager":
          "filemanager", "browser": "browser", "web browser": "browser",
          "email": "browser"}


def open_app(app: str, monitor: str | None = None, workspace=None, *,
             backend=None, index: AppIndex | None = None,
             preferences: dict | None = None, runner=None,
             resolve_monitor=None, env=None) -> dict:
    """Launch an app by desktop id / name (or a role word when the owner's
    config names one). `backend.launch` (when the compositor supports
    placement) gets the resolved monitor name / workspace; otherwise the
    generic detached launcher runs.

    Returns {app, id, launched, monitor?, workspace?, how, note?}.
    """
    from .monitors import resolve_monitor_phrase
    index = index or AppIndex()
    want = str(app or "").strip()
    if not want:
        raise ToolError("open_app: give an app name or desktop id")
    entry = None
    role = _ALIAS.get(want.lower().strip(), want.lower().strip())
    # 1. the owner's configured preference ("my terminal" = their terminal)
    pref = (preferences or {}).get(role) if role in _ALIAS else None
    if pref:
        entry = index.resolve(pref) or ({"id": "", "app": pref, "name": pref,
                                         "exec": pref, "path": ""}
                                        if shutil.which(pref) else None)
    # 2. plain installed-app resolution
    if entry is None:
        entry = index.resolve(want)
    if entry is None and role in _PREFERRED_HINTS and " " not in want and "-" not in want:
        for hint in _PREFERRED_HINTS[role]:
            entry = index.resolve(hint)
            if entry:
                break
    if entry is None:
        near = [a["name"] for a in index.find(want, limit=5)]
        raise ToolError(f"no installed app matches {want!r}"
                        + (f"; close matches: {', '.join(near)}" if near else ""))

    argv = split_exec(entry["exec"])
    if not argv:
        raise ToolError(f"{entry['id'] or want}: empty Exec line")

    reason = block_reason(entry)
    if reason:
        raise ToolError(
            f"open_app: not launching {entry.get('name') or want} — "
            f"{reason}. She never starts your compositor, login session or "
            "system power actions (you own those).")

    mon_name = None
    mon_note = ""
    if monitor and backend is not None and resolve_monitor is not None:
        resolved, err = resolve_monitor_phrase(monitor, resolve_monitor(),
                                               backend_name=backend.name)
        if err:
            raise ToolError(err)
        mon_name = str(resolved.get("name") or "")
    elif monitor:
        mon_note = " (this desktop cannot place windows on a screen; opened anyway)"

    if backend is not None and getattr(backend, "supports_launch", False):
        backend.launch(argv, monitor=mon_name, workspace=workspace,
                       runner=runner, env=env)
        how = f"{backend.name} launch"
    else:
        cmd = generic_launch_command(entry) or argv
        detached_launch(cmd, runner=runner, env=env)
        how = ("gtk-launch/gio (detached)" if cmd is not argv
               else "Exec argv (detached)")

    out = {"app": entry.get("name") or entry["app"], "id": entry.get("id", ""),
           "launched": True, "how": how, "cmd": argv[0]}
    if mon_name:
        out["monitor"] = mon_name
    if workspace not in (None, ""):
        out["workspace"] = workspace
    if mon_note:
        out["note"] = mon_note.strip()
    return out


def detached_launch(argv: list[str], runner=None, env=None) -> str:
    """Run argv detached from us: new session, /dev/null stdio, never a shell."""
    if runner is not None:
        runner(["__launch__", *argv])   # tests: record exactly what would run
        return "detached"
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, env=e)
        return "detached"
    except FileNotFoundError:
        raise ToolError(f"cannot launch {argv[0]!r}: not found on PATH") from None
    except OSError as e:
        raise ToolError(f"cannot launch {argv[0]!r}: {e}") from None


def generic_launch_command(entry: dict) -> list[str] | None:
    """gtk-launch <desktop-id> or gio launch <path> when available."""
    if not entry.get("path"):
        return None
    if shutil.which("gtk-launch"):
        return ["gtk-launch", entry["app"]]
    if shutil.which("gio"):
        return ["gio", "launch", entry["path"]]
    return None
