"""Kate editing tools (docs/TYPING.md, Kate section).

Kate has no document API over D-Bus, so the contract's approach is: drive
the `kate` binary for opening, and edit files on disk (Kate reloads
changed files itself). Real flags were verified against `kate --help`
(Kate 26.x, 2026-11-09) — note two corrections to the contract sketch:

* `-b/--block` blocks until the editor exits, so it is NOT used: plain
  `kate <file>` already reuses the running instance and returns right
  away. Line jump is `-l/--line`, column `-c/--column`.
* there is no "new tab" flag: `-n/--new` starts a whole new *instance*.
  An untitled tab is `kate --tempfile` (opens an empty temporary
  document that is deleted after use); `--startanon` implies `-n`.

`kate_edit` edits on disk with the shared safety helpers (home boundary,
dot-path refusal, `.riko-bak` single backup, exactly-once match) and
shows the owner a real diff through the runtime approval (so its hub
permission default is `always` — the diff prompt IS the ask). It warns
when the tab looks unsaved: Kate marks modified documents with `*` in
the window title, and an editor reload would lose those unsaved edits.

`kate_tabs` reports what is honestly knowable: live Kate windows via
DesktopSense (title = the active document) plus the document lists in
Kate's session files (~/.local/share/kate/sessions/*.katesession,
read-only INI-style `[Document N] URL=…`).

Tests inject `runner=` (never launch Kate) and `sense=None`/fake sense.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

from riko.client_tools import NeedConfirmation, ToolError

KATE_APP_RE = re.compile(r"(^|\.)kate(\.desktop)?$", re.I)   # org.kde.kate
KATE_TITLE_MARK = re.compile(r"\*|\bmodified\b", re.I)


def _sessions_dir(home=None) -> Path:
    h = Path(home) if home else Path(os.environ.get("HOME") or Path.home())
    return h / ".local" / "share" / "kate" / "sessions"


# ── command runner ─────────────────────────────────────────────
def _default_runner(argv):
    """Run the kate binary without blocking on the editor. Tests always
    inject their own fake runner; the conftest guard keeps them from
    reaching a real kate."""
    return subprocess.run([str(a) for a in argv], capture_output=True,
                          timeout=15, text=True)


def _kate_binary(runner):
    if runner is not None:          # fake runners name the binary themselves
        return "kate"
    exe = shutil.which("kate")
    if not exe:
        raise ToolError("Kate isn't installed (ask the owner to install the "
                        "kate package)")
    return exe


def _home_file(path, home=None):
    """Read-side path check: under $HOME (realpath), exists; dot-paths are
    fine to OPEN (the contract restricts writes)."""
    from .files import _home, _inside
    raw = str(path or "").strip()
    if not raw:
        raise ToolError("kate: no file given")
    h = _home(home)
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = h / p
    p = Path(os.path.realpath(str(p)))
    if not _inside(p, h):
        raise ToolError(f"{raw} is outside your home directory — I stay "
                        f"inside {h}")
    return p


# ── tools ──────────────────────────────────────────────────────
def kate_open(path, line=None, column=None, runner=None, home=None, **_kw):
    p = _home_file(path, home)
    if not p.is_file():
        raise ToolError(f"no such file: {p} (kate_new creates one)")
    argv = [_kate_binary(runner)]
    # real flags (kate --help): -l/--line, -c/--column; NO -b — it would
    # block this call until the owner closes the document. Plain `kate f`
    # already joins the running instance.
    if line is not None:
        argv += ["-l", str(int(line))]
    if column is not None:
        argv += ["-c", str(int(column))]
    argv.append(str(p))
    _guard(_run(argv, runner), argv)
    where = f" line {line}" if line is not None else ""
    return f"asked Kate to open {p}{where}"


def kate_new(path=None, text=None, overwrite=False, runner=None,
             home=None, confirmed=False, **_kw):
    from .files import compact_diff, safe_target, write_with_backup
    if path:
        target = safe_target(path, home=home)
        old = target.read_text(encoding="utf-8", errors="replace") \
            if target.exists() else ""
        if old and not overwrite:
            raise ToolError(f"{target} already has content — pass "
                            f"overwrite=true to replace it (asks the owner).")
        if old and not confirmed:
            raise NeedConfirmation(
                f"overwrite {target} with the new text:\n"
                f"{compact_diff(old, str(text or ''))}",
                "kate_new", {"path": str(path), "text": text,
                             "overwrite": True})
        write_with_backup(target, str(text or ""))
        _guard(_run([_kate_binary(runner), str(target)], runner), None)
        return f"wrote {len(str(text or ''))} chars to {target} and opened it"
    # no path: an untitled tab. Verified flag: --tempfile opens an empty
    # temporary document (deleted after use) in the running instance.
    if str(text or ""):
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="riko-untitled-", suffix=".txt",
                                   text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(text))
        argv = [_kate_binary(runner), "--tempfile", tmp]
    else:
        argv = [_kate_binary(runner), "--tempfile"]
    _guard(_run(argv, runner), argv)
    return "opened an untitled tab in Kate" + (
        " with your text" if text else "")


def kate_tabs(sense=None, sessions_dir=None, home=None, **_kw):
    """What Kate has open, honestly: live windows (title = active doc) +
    session-file document lists. Nothing is invented."""
    out: dict = {"windows": [], "sessions": [], "session_dir":
                 str(sessions_dir or _sessions_dir(home))}
    if sense is not None:
        try:
            rows = (sense.desktop_windows() or {}).get("windows") or []
            for w in rows:
                app = str((w or {}).get("app") or "")
                if KATE_APP_RE.search(app):
                    title = str(w.get("title") or "")
                    out["windows"].append(
                        {"title": title,
                         "unsaved": bool(KATE_TITLE_MARK.search(title)),
                         "focused": bool(w.get("focused"))})
        except Exception as e:   # no backend / awareness off: say so
            out["windows_note"] = f"live Kate windows unavailable: {str(e)[:120]}"
    else:
        out["windows_note"] = ("desktop awareness is off — open windows "
                               "unknown (session files only)")
    d = Path(out["session_dir"])
    if d.is_dir():
        for f in sorted(d.glob("*.katesession")):
            docs, untitled = _parse_session(f)
            out["sessions"].append({"session": f.name, "documents": docs,
                                    "untitled_documents": untitled})
        if not out["sessions"]:
            out["sessions_note"] = "no .katesession files found"
    else:
        out["sessions_note"] = f"no session dir at {d}"
    return out


def _parse_session(path: Path):
    """INI-ish Kate session: [Document N] sections with URL= lines.
    Empty URL = an untitled document. Read-only."""
    docs, untitled = [], 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return docs, untitled
    in_doc = False
    seen_url = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            if in_doc and not seen_url:
                untitled += 1
            in_doc, seen_url = line.lower().startswith("[document"), False
            continue
        if in_doc and line.lower().startswith("url="):
            seen_url = True
            url = line[4:].strip()
            if url:
                docs.append(_url_to_path(url))
            else:
                untitled += 1     # Kate writes URL= for untitled docs
    if in_doc and not seen_url:
        untitled += 1
    return docs, untitled


def _url_to_path(url: str) -> str:
    if url.startswith("file://"):
        return unquote(urlparse(url).path)
    return url


def _run(argv, runner):
    try:
        if runner is not None:
            return runner(argv)
        return _default_runner(argv)
    except subprocess.TimeoutExpired:
        raise ToolError("the kate command never returned") from None
    except OSError as e:
        raise ToolError(f"couldn't run kate: {e}") from None


def _guard(done, argv):
    if done is None:
        return
    rc = getattr(done, "returncode", 0)
    if rc not in (0, None):
        err = str(getattr(done, "stderr", "") or "").strip()[:200]
        raise ToolError(f"kate failed ({rc}): {err or ' '.join(str(a) for a in (argv or []))}")


def _unsaved_warning(path: Path, sense):
    """True when a Kate window seems to hold unsaved edits for this file."""
    if sense is None:
        return False
    try:
        rows = (sense.desktop_windows() or {}).get("windows") or []
    except Exception:
        return False
    for w in rows:
        if not w or not KATE_APP_RE.search(str(w.get("app") or "")):
            continue
        title = str(w.get("title") or "")
        if path.name in title and KATE_TITLE_MARK.search(title):
            return True
    return False


def kate_edit(path, find, replace, allow_dot=False, runner=None,
              home=None, sense=None, confirmed=False, **_kw):
    """On-disk exact-match edit; the owner approves a real diff first."""
    from .files import (apply_edit_once, compact_diff, safe_target,
                        write_with_backup)
    target = safe_target(path, home=home, allow_dot=allow_dot,
                         must_exist=True)
    if not target.is_file():
        raise ToolError(f"{target} is not a regular file")
    old = target.read_text(encoding="utf-8", errors="replace")
    new = apply_edit_once(old, find, replace)
    if not confirmed:
        warn = ""
        if _unsaved_warning(target, sense):
            warn = ("\n⚠ the Kate tab for this file looks UNSAVED (title "
                    "shows a modified marker) — saving inside Kate would "
                    "still win; continue only if that's ok.")
        raise NeedConfirmation(
            f"edit file {target}:\n{compact_diff(old, new)}{warn}",
            "kate_edit", {"path": str(path), "find": find,
                          "replace": replace, "allow_dot": allow_dot})
    bak = write_with_backup(target, new)
    note = f" (previous content kept in {bak.name})" if bak else ""
    if _unsaved_warning(target, sense):
        note += (" — WARNING: the open Kate tab had unsaved changes; "
                 "check it didn't just overwrite this edit")
    return f"edited {target}: replaced 1 occurrence{note}"


# ── catalogue ──────────────────────────────────────────────────
def _obj(props, required=None):
    spec = {"type": "object", "additionalProperties": False,
            "properties": props}
    if required:
        spec["required"] = list(required)
    return spec


def kate_tools():
    return [
        {"name": "kate_open", "risk": "act", "permission_default": "always",
         "description": "Open an existing file (under $HOME) in the running "
                        "Kate window, optionally at a line/column.",
         "parameters": _obj({"path": {"type": "string"},
                             "line": {"type": "integer"},
                             "column": {"type": "integer"}},
                            required=["path"])},
        {"name": "kate_new", "risk": "act",
         "description": "Create a new file (path under $HOME, refuses to "
                        "replace existing content unless overwrite=true) and "
                        "open it; without a path, opens an untitled Kate "
                        "tab, optionally holding the given text.",
         "parameters": _obj({"path": {"type": "string"},
                             "text": {"type": "string"},
                             "overwrite": {"type": "boolean"}})},
        {"name": "kate_tabs", "risk": "read",
         "description": "What Kate has open: window titles from the desktop "
                        "(the active document, with unsaved markers) plus "
                        "document lists from Kate's session files.",
         "parameters": _obj({})},
        {"name": "kate_edit", "risk": "act", "permission_default": "always",
         "description": "Exact-match edit of a file on disk (Kate reloads "
                        "it): `find` must match exactly once or nothing "
                        "changes; the owner sees a diff first. Keeps one "
                        ".riko-bak backup of the previous content.",
         "parameters": _obj({"path": {"type": "string"},
                             "find": {"type": "string"},
                             "replace": {"type": "string"},
                             "allow_dot": {"type": "boolean",
                                           "description": "only after the "
                                                          "owner approved "
                                                          "this exact dot-"
                                                          "path"}},
                            required=["path", "find", "replace"])},
    ]


def kate_executors(sense=None, home=None):
    from functools import partial
    from .files import find_file
    ex = {}
    for t in kate_tools():
        ex[t["name"]] = partial(globals()[t["name"]], sense=sense, home=home)
    ex["find_file"] = partial(find_file, home=home)
    return ex


def kate_find_tools():
    return [
        {"name": "find_file", "risk": "read",
         "description": "Find files by name under the owner's home "
                        "(default roots ~/Documents ~/Development ~/Cloud "
                        "~/Desktop; hidden/.git/node_modules/caches are "
                        "skipped). Name matches rank first; max 30; never "
                        "leaves $HOME.",
         "parameters": _obj({"query": {"type": "string"},
                             "under": {"type": "string",
                                       "description": "search under this "
                                                      "directory instead "
                                                      "(must stay in $HOME)"}},
                            required=["query"])},
    ]
