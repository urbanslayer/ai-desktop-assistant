"""File-side helpers for the app-editing tools (docs/TYPING.md).

One home for the rules every write tool shares:

* **Home boundary** — writes and `find_file` scans never leave $HOME.
* **Dot-path refusal** — anything under a dot-directory (`~/.config`,
  `~/.ssh`, `~/.local`, …) is refused unless the caller passes the
  owner's exact-path approval (`allow_dot=True` — the approval bubble
  must name the literal path first, per the contract).
* **`.riko-bak`** — every on-disk edit keeps a single backup of the
  previous content next to the file (most recent only, same name each
  time).
* **Exact-match edits** — `find` must occur exactly once or the edit
  fails with the count; the approval shows a compact unified diff.
* **`find_file`** — ranked name search over the owner's working roots.

All functions take their roots (`home`) as optional arguments so tests
run entirely in tmp dirs; nothing here shells out.
"""
from __future__ import annotations

import difflib
import os
import re
import time
from pathlib import Path

from riko.client_tools import ToolError

BACKUP_SUFFIX = ".riko-bak"
HOME = Path.home()

# find_file: contract defaults (docs/TYPING.md)
DEFAULT_ROOTS = ("Documents", "Development", "Cloud", "Desktop")
MAX_RESULTS = 30
# A full walk of ~/Development can hit huge trees; these keep one call
# bounded (truncated scans say so in the result instead of lying).
MAX_SCANNED = 60_000
MAX_DEPTH = 12

_SKIP_DIRS = {"node_modules", "__pycache__", "cache", "caches",
              "site-packages", "venv", ".venv", "dist", "build"}


def _home(home=None) -> Path:
    h = Path(home) if home else Path(os.environ.get("HOME") or HOME)
    return h.resolve()


def safe_target(path, home=None, allow_dot=False, must_exist=False):
    """Resolve a model-supplied path for a WRITE.

    Relative paths resolve under $HOME. The result must stay inside
    $HOME (realpath-checked, symlinks included) and must not pass
    through a dot-directory unless `allow_dot` (the exact-path approval)
    is set. Returns the resolved Path; raises ToolError with the reason.
    """
    raw = str(path or "").strip()
    if not raw:
        raise ToolError("no path given")
    h = _home(home)
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = h / p
    # lexically clamp first so we never realpath outside home at all
    try:
        lex = Path(os.path.normpath(str(p)))
    except (TypeError, ValueError):
        raise ToolError(f"bad path: {raw!r}") from None
    if not _inside(lex, h):
        raise ToolError(f"{raw} is outside your home directory — I only "
                        f"touch files under {h}")
    resolved = Path(os.path.realpath(str(lex)))
    if not _inside(resolved, h):
        raise ToolError(f"{raw} resolves outside your home directory "
                        f"(symlink to {resolved}) — refused")
    rel = resolved.relative_to(h)
    if not allow_dot and any(part.startswith(".") for part in rel.parts[:-1]):
        raise ToolError(
            f"{raw} lives in a hidden/config directory — I won't write "
            f"there unless the owner names the exact path "
            f"({resolved}) in the approval (pass allow_dot=true then)")
    if must_exist and not resolved.exists():
        raise ToolError(f"no such file: {resolved}")
    return resolved


def _inside(p, root: Path) -> bool:
    try:
        Path(p).relative_to(root)
        return True
    except ValueError:
        return False


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def write_with_backup(path, content) -> Path | None:
    """Write `content` (str/bytes) to `path`, first copying the previous
    content to <name>.riko-bak (single slot: newest wins). Returns the
    backup path or None when the file did not exist."""
    path = Path(path)
    bak = None
    if path.exists():
        bak = backup_path(path)
        bak.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    tmp = path.with_name(path.name + ".riko-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return bak


def apply_edit_once(text, find, replace):
    """Exact-match, once-only replacement. Returns the new text; raises
    ToolError naming the occurrence count when it is not exactly 1."""
    find = str(find or "")
    if not find:
        raise ToolError("find is empty — tell me what to look for")
    n = text.count(find)
    if n == 0:
        raise ToolError("found 0 matches — the text I was asked to replace "
                        "isn't in the file (it may have changed since you "
                        "read it)")
    if n > 1:
        raise ToolError(f"found {n} matches, not 1 — give me a longer, "
                        f"unique snippet so I replace the right one")
    return text.replace(find, str(replace or ""))


def compact_diff(old, new, max_lines=24, context=2):
    """A small unified diff for the approval bubble."""
    lines = list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile="before", tofile="after", lineterm="", n=context))
    if not lines:
        return "(no text change)"
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"… {omitted} more diff line(s) not shown"]
    return "\n".join(lines)


# ── find_file ─────────────────────────────────────────────────
def _score(name: str, stem: str, q: str, qci: str) -> int | None:
    if name == q or stem == q:
        return 0                       # exact name
    if name.lower() == qci or stem.lower() == qci:
        return 1                       # case-insensitive exact
    if name.lower().startswith(qci) or stem.lower().startswith(qci):
        return 2                       # prefix
    if qci in name.lower() or qci in stem.lower():
        return 3                       # substring
    return None                        # basename miss (path match handled)


def find_file(query, under=None, limit=MAX_RESULTS, home=None):
    """Ranked filename search, never leaving $HOME (docs/TYPING.md).

    Default roots: ~/Documents ~/Development ~/Cloud ~/Desktop (existing
    ones). Hidden dirs, .git, node_modules and caches are skipped. Name
    matches rank first; directory-path matches only fill leftover slots.
    """
    q = str(query or "").strip()
    if not q:
        raise ToolError("find_file: give a name or phrase to look for")
    h = _home(home)
    limit = max(1, min(int(limit or MAX_RESULTS), MAX_RESULTS))
    qci = q.lower()

    if under not in (None, ""):
        root = Path(os.path.expanduser(str(under)))
        if not root.is_absolute():
            root = h / root
        root = Path(os.path.normpath(str(root)))
        if not _inside(root, h):
            raise ToolError(f"find_file: {under} is outside your home "
                            f"directory — I only search under {h}")
        if not root.is_dir():
            raise ToolError(f"find_file: no such directory: {root}")
        roots = [root]
    else:
        roots = [h / r for r in DEFAULT_ROOTS if (h / r).is_dir()]
        if not roots:
            roots = [h]

    name_hits: list[tuple] = []
    path_hits: list[tuple] = []
    scanned = 0
    truncated = False
    for root in roots:
        base_len = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dp = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d not in _SKIP_DIRS)
            depth = len(dp.parts) - base_len
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            for fn in filenames:
                scanned += 1
                if scanned > MAX_SCANNED:
                    truncated = True
                    break
                if fn.startswith("."):
                    continue
                score = _score(fn, os.path.splitext(fn)[0], q, qci)
                full = dp / fn
                if score is not None:
                    name_hits.append((score, depth, len(fn), str(full)))
                elif qci in str(dp).lower():
                    path_hits.append((9, depth, len(fn), str(full)))
            if truncated:
                break
        if truncated:
            break

    name_hits.sort()
    path_hits.sort()
    out = []
    for row in (name_hits + path_hits)[:limit]:
        try:
            st = Path(row[3]).stat()
            out.append({"path": row[3], "size": st.st_size,
                        "modified": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        except OSError:
            out.append({"path": row[3]})
    result = {"query": q, "results": out, "roots": [str(r) for r in roots]}
    if truncated:
        result["note"] = (f"scan hit the {MAX_SCANNED}-file limit — there "
                          f"may be more matches; narrow the search")
    return result
