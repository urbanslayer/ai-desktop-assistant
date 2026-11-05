"""The generic app layer: XDG .desktop index + the safe launcher (contract §5).

Rules from docs/DESKTOP_SENSE.md:
- only desktop ids / Exec lines from INSTALLED .desktop files ever run;
  the model never gets to hand us a shell string;
- field codes (%u %U %f %F %d %D %n %N %i %c %k %v %m) are stripped from
  Exec, quotes are unquoted per the desktop-entry spec, and the result runs
  as an argv list (no shell), so `foo; rm -rf /` in a Name/Exec is data;
- launches are detached (start_new_session, /dev/null stdio) and inherit
  the owner's environment.
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


class AppIndex:
    """Installed launchable apps, fuzzy-searched. Reads the XDG dirs lazily
    per call (apps get installed and uninstalled); `dirs` pins them (tests)."""

    def __init__(self, dirs=None):
        self._dirs = [Path(d) for d in dirs] if dirs else None

    def _search_dirs(self) -> list[Path]:
        return self._dirs or xdg_app_dirs()

    def all(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for d in self._search_dirs():
            try:
                entries = sorted(d.glob("*.desktop"))
            except OSError:
                continue
            for f in entries:
                app = parse_desktop_file(f)
                if app and app["id"] not in seen:   # user dir wins (first)
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
        confident match (callers must NOT launch a weak guess)."""
        want_l = str(want or "").strip().lower()
        want_l = re.sub(r"\.desktop$", "", want_l)
        if not want_l:
            return None
        apps = self.all()
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

    mon_name = None
    mon_note = ""
    if monitor and backend is not None and resolve_monitor is not None:
        mon_name, err = resolve_monitor_phrase(monitor, resolve_monitor(),
                                               backend_name=backend.name)
        if err:
            raise ToolError(err)
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
