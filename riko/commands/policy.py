"""Pure decision logic for run_command (docs/COMMANDS.md).

`decide()` answers one question without running anything: may this exact
command run UNATTENDED (mode "auto"), must the owner see it first (mode
"ask"), or is it refused outright even with approval (mode "refuse")?

Rules (docs/COMMANDS.md 'Execution rules' + 'Auto-run allowlist'):
  * never elevated: sudo/su/doas/pkexec/run0 and `machinectl shell` refuse;
  * never a compositor/session/power lever: the open_app denylist
    (riko/desktop_sense/apps.block_reason) plus hyprctl/swaymsg/loginctl,
    the systemctl power verbs, shutdown/reboot/poweroff/halt, and
    kill/pkill/killall aimed at session processes (systemctl keeps its
    read-only verbs — that is where block_reason is too coarse);
  * never a disk destroyer: mkfs*, dd of=/dev/*, wipefs, shred /dev/*,
    rm -r/-f on /, ~, or any path outside $HOME and /tmp;
  * those refusals hold EVEN WITH approval, so they are applied by a
    recursive scan: wrapper programs (env/nice/ionice/nohup/timeout/time/
    stdbuf/chrt/taskset/setsid/flock/unbuffer/script -c/watch/xargs/
    find -exec/busybox/command/exec/...) are unwrapped to the real
    program, and shell strings (bash -c '...', and this tool's own
    shell-syntax path) are split into segments on ; && || | & newlines
    plus $()/backticks/() and scanned segment by segment. A structure
    the best-effort lexer cannot parse safely is REFUSED, not waved
    through; absolute/relative paths to refused binaries count too
    (_tok_name basenames);
  * never interactive (vim/less/top/ssh-without-command/REPL/man …);
  * no shell by default: shell syntax forces approval + `bash -c`;
  * auto-run only for allowlisted read-only programs over clean arguments
    (no secret path, no write flag, cwd inside $HOME or /tmp); auto-run
    git runs with -c core.fsmonitor=false -c core.hooksPath=/dev/null and
    (diff/show/log -p) --no-ext-diff --no-textconv, so no repo-configured
    external program ever executes unattended.
"""
from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field

from ..desktop_sense.apps import (_SESSION_IDS, _is_session_name, _tok_name,
                                  block_reason)

DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 120.0

# ── outright refusals (approval does not unlock these) ─────────
ELEVATE = {"sudo", "su", "doas", "pkexec", "run0"}
SESSION_EXTRA = {"hyprctl", "swaymsg", "loginctl"}
POWER_VERBS = {"poweroff", "reboot", "suspend", "hibernate", "halt", "kexec"}
DESTROY_PREFIX = "mkfs"
DESTROY_BARE = {"wipefs"}

# known interactive / full-screen programs (docs/COMMANDS.md rule 4)
INTERACTIVE = {
    "vim": "cat (or pass a script/ex command)",
    "nvim": "cat (or pass a script/ex command)",
    "vi": "cat",
    "nano": "cat",
    "emacs": "cat with -Q --batch",
    "pico": "cat",
    "less": "cat",
    "more": "cat",
    "most": "cat",
    "man": "tldr/man -w, or 'man <page> | col -b' with approval",
    "top": "top -b -n1",
    "htop": "top -b -n1",
    "btop": "top -b -n1",
}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl"}

# argument markers that name a secret path (component sequences, matched
# against raw AND fully-expanded argv/cwd paths; docs/COMMANDS.md rule 7)
_SECRET_COMPONENTS = (
    (".ssh",), (".gnupg",), (".password-store",),
    (".local", "share", "keyrings"),
    (".mozilla",), (".thunderbird",),
    (".config", "google-chrome"), (".config", "chromium"),
    (".config", "riko"),
    (".netrc",), (".git-credentials",),
    (".aws",), (".kube",), (".docker", "config.json"),
)
_SECRET_EXACT = ("/etc/shadow",)

# ── auto-run allowlist (docs/COMMANDS.md) ──────────────────────
_BARE_AUTO = {"df", "free", "uptime", "whoami", "id", "pwd", "date", "uname",
              "hostname", "nproc", "lscpu", "lsblk", "lsusb", "lspci",
              "sensors", "which", "type", "file", "stat", "ps", "ls",
              "findmnt", "checkupdates"}
# cat-family: only on regular files <= 1 MB under $HOME or /etc
_READ_FILES = {"cat", "head", "tail", "wc", "grep", "rg"}
_READ_FILE_MAX = 1_000_000
_SYSTEMCTL_READ_USER = {"status", "list-units", "list-timers", "is-active",
                        "is-enabled"}
_SYSTEMCTL_READ_SYS = {"status", "list-units", "is-active"}
_GIT_READ = {"status", "log", "diff", "show", "branch", "remote"}


@dataclass
class Decision:
    command: str
    mode: str = "ask"            # auto | ask | refuse
    reason: str = ""             # refuse reason / why approval is needed
    argv: list = field(default_factory=list)   # what would actually exec
    shell: bool = False
    cwd: str = ""
    exists: bool = True          # cwd exists
    timeout: float = DEFAULT_TIMEOUT
    secret_touch: bool = False   # args touch a secret path (approval warns)
    timed_out_default: bool = False


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError as e:
        raise ValueError(f"the command does not parse cleanly ({e})") from None


def has_shell_syntax(command: str) -> bool:
    """Shell metacharacters OUTSIDE quotes (pipes, ;, &&, redirects, $(),
    backticks, globs). A quoted '|' inside an argument is literal text."""
    quote = ""
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch in "|;&<>()$`*?[":
            return True
    return False


def expand(token, home: str) -> str:
    """~ / $HOME / vars expanded (the ~ of THIS home param, not the live
    one — tests pass a fake home), then normalised."""
    t = str(token)
    if t == "~" or t.startswith("~/"):
        t = home + t[1:]
    t = os.path.expandvars(t)
    t = t.replace("$HOME", home)
    if t == "~" or t.startswith("~/"):
        t = home + t[1:]
    return os.path.normpath(t) if "/" in t or t.startswith("~") else t


def touches_secret(token, home: str) -> bool:
    raw = str(token)
    if raw == "-" or not raw:
        return False
    abspath = os.path.normpath(os.path.join(os.sep, expand(raw, home)))
    for variant in (raw, expand(raw, home), abspath):
        parts = [p for p in variant.replace("\\", "/").split("/") if p]
        posix = "/" + "/".join(parts)
        if posix in _SECRET_EXACT:
            return True
        for comp in _SECRET_COMPONENTS:
            n = len(comp)
            for i in range(len(parts) - n + 1):
                if tuple(parts[i:i + n]) == comp:
                    return True
        base = parts[-1] if parts else ""
        if base == ".env" or base.startswith(".env."):
            return True
    return False


def _deny_elevation(argv: list[str]) -> str | None:
    if not argv:
        return None
    prog = _tok_name(argv[0])
    if prog in ELEVATE:
        return f"never elevated ({prog})"
    if prog == "machinectl" and "shell" in _paths(argv[1:]):
        return "never elevated (machinectl)"
    return None


def _flags(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t.startswith("-")]


def _paths(tokens: list[str]) -> list[str]:
    return [t for t in tokens if not t.startswith("-")]


def _deny_session_power(argv: list[str]) -> str | None:
    """Compositor/session/power denylist: block_reason (the open_app one)
    plus this tool's extensions. systemctl keeps its read-only verbs."""
    if not argv:
        return None
    first = _tok_name(argv[0])
    if first in SESSION_EXTRA:
        return f"session control command ({first})"
    if first == "systemctl":
        verbs = _paths(argv[1:])
        verb = verbs[0] if verbs else ""
        if verb in POWER_VERBS:
            return f"system power/session command (systemctl {verb})"
        return None
    entry = {"exec": " ".join(shlex.quote(t) for t in argv),
             "path": "", "app": "", "id": ""}
    reason = block_reason(entry)
    if reason and "systemctl" not in reason:
        return reason
    if first in ("kill", "pkill", "killall"):
        # tokens can be command-substitution text (`kill $(pgrep Hyprland)`
        # in shell mode arrives as "$(pgrep" / "Hyprland)"), so scan every
        # word-shaped fragment of the argv, not just argv tokens.
        words = re.findall(r"[A-Za-z0-9._-]+", " ".join(argv[1:]))
        for w in words:
            name = _tok_name(w)
            if name in _SESSION_IDS or _is_session_name(name):
                return f"kills the session/compositor process ({name})"
    return None


def _deny_interactive(argv: list[str]) -> str | None:
    if not argv:
        return None
    first = _tok_name(argv[0])
    rest = argv[1:]
    if first in INTERACTIVE:
        if first == "top" and any(t in ("-b", "-c") or t.startswith("-b")
                                  for t in _flags(rest)) and "-n" in " ".join(
                                      _flags(rest)):
            return None                    # top -b -n1 is batch mode
        if first in ("less", "more", "most") and "-F" in " ".join(_flags(rest)):
            return None                    # quit-after-one-screen
        return (f"{first} is interactive/full-screen (no TTY here) — use "
                f"{INTERACTIVE[first]} instead")
    if first in INTERPRETERS:
        joined = " ".join(_flags(rest))
        if not _paths(rest) and not any(
                v in joined for v in ("-c", "-m", "-e", "--version", "-V",
                                      "--help")):
            return (f"{first} without a script would open a REPL (no TTY "
                    f"here) — pass a script or -c")
    if first == "ssh" and not _paths(rest):
        return "ssh without a command opens an interactive session (no TTY here)"
    return None


def _deny_destroyer(argv: list[str], cwd: str, home: str) -> str | None:
    if not argv:
        return None
    first = _tok_name(argv[0])
    rest = argv[1:]
    if first.startswith(DESTROY_PREFIX):
        return f"{first} formats a disk"
    if first in DESTROY_BARE:
        return f"{first} wipes filesystem signatures"
    if first == "dd" and any(t.startswith("of=/dev/") for t in rest):
        return "dd writes straight to a device"
    if first == "shred" and any(expand(t, home).startswith("/dev/")
                               for t in _paths(rest)):
        return "shred destroys a device"
    if first == "rm":
        flags = "".join(f.lstrip("-") for f in _flags(rest))
        if "r" in flags or "R" in flags or "f" in flags:
            for tok in _paths(rest):
                p = _resolve(tok, cwd, home)
                if p == "/" or not (
                        p.startswith(home + "/")
                        or p == "/tmp" or p.startswith("/tmp/")):
                    return (f"rm -r/-f on '{tok}' (outside $HOME and /tmp, "
                            f"or on $HOME itself)")
    return None


def _resolve(token: str, cwd: str, home: str) -> str:
    p = expand(token, home)
    return p if p.startswith("/") else os.path.normpath(os.path.join(cwd, p))


def _under(token: str, cwd: str, home: str, roots) -> bool:
    p = _resolve(token, cwd, home)
    return any(p == r or p.startswith(r + "/") for r in roots)


def _read_files_auto(tokens: list[str], cwd: str, home: str) -> bool:
    """cat/head/tail/wc/grep/rg auto-run only over small regular files
    under $HOME or /etc (not secret paths). A missing/oddly-typed path or
    no file at all (stdin) means: ask."""
    roots = (home, "/etc")
    files = [t for t in _paths(tokens) if t != "-"]
    checked = existing = 0
    for tok in files:
        p = _resolve(tok, cwd, home)
        try:
            st = os.stat(p)
        except OSError:
            continue                     # could be a grep pattern
        existing += 1
        checked += 1
        if not os.path.isfile(p) or st.st_size > _READ_FILE_MAX \
                or not _under(tok, cwd, home, roots) \
                or touches_secret(tok, home):
            return False
    return existing >= 1 and checked == existing


# ── refusal scan: wrapper programs + shell strings ─────────────
# docs/COMMANDS.md rule 1: the refusals hold EVEN WITH approval, so a
# refusal may not hide behind a wrapper (`env sudo ls`, `timeout 5 sudo
# ls`, `xargs sudo ...`) or inside a shell string (`bash -c "ls; rm -rf
# ~"`, `ls && sudo x`, `$(sudo ...)`). The scan below sees through both,
# recursively; a structure it cannot parse safely is REFUSED, never
# waved through.

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}

class _ShellUncertain(Exception):
    """Shell structure this best-effort lexer cannot check safely."""


def _placeholder(inner: str, op: str, cl: str) -> str:
    """Sentinel-wrapped stand-in for $( )/backtick/process-substitution
    content: the words stay (the session-kill word scan still sees them);
    only quote chars go, so the outer tokenizer can't misread them."""
    return op + re.sub(r"['\"`]", "", str(inner)) + cl


def _quoted_span(text: str, i: int) -> int:
    """text[i] is ' or "; return the index just past the closing quote."""
    q = text[i]
    j, n = i + 1, len(text)
    while j < n:
        if text[j] == "\\" and q == '"' and j + 1 < n:
            j += 2
            continue
        if text[j] == q:
            return j + 1
        j += 1
    raise _ShellUncertain("unbalanced quote")


def _paren_span(text: str, i: int) -> tuple[str, int]:
    """text[i] == '(' (already quote-stripped OUTSIDE, quotes INSIDE are
    honoured): return (content, index just past the matching ')')."""
    depth, j, n = 0, i, len(text)
    while j < n:
        ch = text[j]
        if ch in "'\"":
            j = _quoted_span(text, j)
            continue
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    raise _ShellUncertain("unbalanced ( )")


def _dollar_paren_span(text: str, i: int) -> tuple[str | None, int]:
    """text starts with '$(' at i. Arithmetic $(( )) yields None (no
    command can hide in it); anything else returns its content."""
    if text.startswith("$((", i):
        end = text.find("))", i + 3)
        if end < 0:
            raise _ShellUncertain("unbalanced $(( ))")
        return None, end + 2
    return _paren_span(text, i + 1)


def _extract_subs(text: str) -> tuple[str, list[str]]:
    """Replace every $( ) / backtick / <( ) command region with a
    space-free placeholder; return (cleaned text, region contents)."""
    out: list[str] = []
    inner: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = _quoted_span(text, i)          # no expansion in '...'
            out.append(text[i:j])
            i = j
            continue
        if ch == '"':
            j = i + 1
            buf = ['"']
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if text[j] == "`":
                    k = text.find("`", j + 1)
                    if k < 0:
                        raise _ShellUncertain("unbalanced backtick")
                    inner.append(text[j + 1:k])
                    buf.append(_placeholder(text[j + 1:k], "\x01", "\x02"))
                    j = k + 1
                    continue
                if text.startswith("$(", j):
                    sub, j2 = _dollar_paren_span(text, j)
                    if sub is not None:
                        inner.append(sub)
                    buf.append(_placeholder(sub or "arith", "\x01", "\x02"))
                    j = j2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise _ShellUncertain("unbalanced \" quote")
            out.append("".join(buf) + '"')
            i = j + 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == "`":
            k = text.find("`", i + 1)
            if k < 0:
                raise _ShellUncertain("unbalanced backtick")
            inner.append(text[i + 1:k])
            out.append(_placeholder(text[i + 1:k], "\x01", "\x02"))
            i = k + 1
            continue
        if text.startswith("$(", i):
            sub, j2 = _dollar_paren_span(text, i)
            if sub is not None:
                inner.append(sub)
            out.append(_placeholder(sub or "arith", "\x01", "\x02"))
            i = j2
            continue
        if ch in "<>" and i + 1 < n and text[i + 1] == "(":
            sub, j2 = _paren_span(text, i + 1)     # process substitution
            inner.append(sub)
            out.append(ch + _placeholder(sub, "\x03", "\x04"))
            i = j2
            continue
        out.append(ch)
        i += 1
    return "".join(out), inner


def _split_segments(text: str) -> list[list[str]]:
    """Split command text into segments on | ; && || & newlines and
    (subshell parens); redirect operators and their targets are dropped.
    Assumes $( )/backticks are already placeholders (_extract_subs)."""
    segs: list[list[str]] = []
    cur: list[str] = []
    tok: list[str] = []
    i, n = 0, len(text)

    def end_tok():
        if tok:
            v = "".join(tok)
            tok.clear()
            if v:
                cur.append(v)

    def end_seg():
        end_tok()
        if cur:
            segs.append(list(cur))
            cur.clear()

    def skip_target():
        nonlocal i
        while i < n and text[i] in " \t":
            i += 1
        while i < n and text[i] not in " \t\n":
            if text[i] in "'\"":
                i = _quoted_span(text, i)
                continue
            i += 1

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in " \t\r\f\v":
            end_tok()
            i += 1
            continue
        if ch == "\n":
            end_tok()
            end_seg()
            i += 1
            continue
        if ch in "\x01\x02\x03\x04":
            tok.append(ch)
            i += 1
            continue
        if ch in "'\"":
            j = _quoted_span(text, i)
            tok.append(text[i + 1:j - 1])       # content, quotes dropped
            i = j
            continue
        if ch == "\\":
            if i + 1 < n:
                tok.append(nxt)
                i += 2
            else:
                i += 1
            continue
        if ch == ";":
            end_tok()
            end_seg()
            i += 1
            continue
        if ch == "|":
            end_tok()
            end_seg()
            i += 2 if nxt in "&|" else 1
            continue
        if ch == "&":
            if nxt == ">":                      # cmd &>file: one redirect
                end_tok()
                i += 2
                skip_target()
                continue
            end_tok()
            end_seg()
            i += 2 if nxt == "&" else 1
            continue
        if ch == "(":
            if nxt == "(":
                raise _ShellUncertain("arithmetic (( )) syntax")
            end_tok()
            end_seg()
            i += 1
            continue
        if ch == ")":
            end_tok()
            end_seg()
            i += 1
            continue
        if ch == "<":
            if nxt == "<":
                raise _ShellUncertain("here-document")
            end_tok()
            i += 1
            if i < n and text[i] == "&":
                i += 1
            skip_target()
            continue
        if ch == ">":
            end_tok()
            i += 1
            if i < n and text[i] == ">":
                i += 1
            if i < n and text[i] == "&":
                i += 1
            skip_target()
            continue
        tok.append(ch)
        i += 1
    end_seg()
    return segs


def _lex_shell(text: str) -> list[list[str]]:
    """Best-effort: a shell string (or the tool's own shell-syntax path)
    into command segments, including the commands inside $( ), backticks,
    <( ) and (subshells). Raises _ShellUncertain when the structure can
    not be checked safely."""
    cleaned, inner = _extract_subs(str(text))
    segs = _split_segments(cleaned)
    for sub in inner:
        segs.extend(_lex_shell(sub))
    return segs


class _Wrapper:
    """Option spec for a wrapper program: which flags are boolean, which
    take a value (their argument is skipped too), which flag's value is a
    SHELL STRING to lex (env -S, script -c, flock -c), whether leading
    NAME=value assignments count as options (env), whether the whole
    payload is only the shell-string value (script), and whether the
    first positional is data before the real program (timeout duration,
    flock file, taskset mask, chrt priority)."""
    __slots__ = ("bools", "values", "shell_value", "assign",
                 "needs_shell_value", "skip_first")

    def __init__(self, bools=(), values=(), shell_value=(), assign=False,
                 needs_shell_value=False, skip_first=False):
        self.bools = set(bools)
        self.values = set(values)
        self.shell_value = set(shell_value)
        self.assign = assign
        self.needs_shell_value = needs_shell_value
        self.skip_first = skip_first


_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_WRAPPERS = {
    "env": _Wrapper(bools=("-i", "--ignore-environment", "-0", "--null",
                           "-v", "--debug", "-b"),
                    values=("-u", "--unset", "--chdir"),
                    shell_value=("-S",), assign=True),
    "nice": _Wrapper(values=("-n", "-a")),
    "ionice": _Wrapper(bools=("-k", "--ignore"),
                       values=("-c", "-n", "-p", "--class", "--priority",
                               "--pid")),
    "nohup": _Wrapper(),
    "time": _Wrapper(bools=("-p", "-v", "-q", "-a", "--quiet", "--verbose",
                            "--portability", "--append", "--shell"),
                     values=("-o", "-f", "--output", "--format")),
    "stdbuf": _Wrapper(bools=("-F", "--fork"),
                       values=("-o", "-i", "-e", "--output", "--input",
                               "--error")),
    "setsid": _Wrapper(bools=("-c", "--ctty", "-f", "--fork", "-w",
                              "--wait")),
    "unbuffer": _Wrapper(),
    "chrt": _Wrapper(bools=("-p", "-f", "-r", "-i", "-o", "-b", "-R",
                            "--fifo", "--rr", "--idle", "--other",
                            "--batch", "--reset-policy"),
                     skip_first=True),
    "taskset": _Wrapper(bools=("-p", "-a", "-g"),
                        values=("-c", "--cpu-list"), skip_first=True),
    "flock": _Wrapper(bools=("-n", "--nonblock", "-s", "--shared",
                             "-u", "--unlock", "-x", "--exclusive"),
                      values=("-w", "--timeout", "--fd"),
                      shell_value=("-c", "--command"), skip_first=True),
    "timeout": _Wrapper(bools=("-v", "--verbose", "--preserve-status",
                               "--foreground"),
                        values=("-k", "--kill-after", "-s", "--signal"),
                        skip_first=True),
    "xargs": _Wrapper(bools=("-0", "--null", "-t", "--verbose", "-r",
                             "--no-run-if-empty", "-o", "--open-tty",
                             "-x", "--exit"),
                      values=("-a", "-d", "-I", "-E", "-n", "-L", "-P",
                              "-s", "--delimiter", "--replace", "--eof",
                              "--max-args", "--max-lines", "--max-procs",
                              "--max-chars", "--argv-file")),
    "watch": _Wrapper(bools=("-d", "--differences", "-t", "--no-title",
                             "-b", "--beep", "-e", "--exec", "-x", "-g",
                             "-c", "--color", "-f", "--follow"),
                      values=("-n", "--interval", "-w", "--watchinterval",
                              "--chginterval", "--label")),
    "script": _Wrapper(bools=("-e", "--return", "--force", "-q", "--quiet",
                              "-f", "--flush", "-r", "--responsive"),
                       values=("-o", "--log", "-O", "--log-out",
                               "-T", "--timing", "-E", "--exit"),
                       shell_value=("-c", "--command"),
                       needs_shell_value=True),
    "command": _Wrapper(bools=("-v", "-V"), values=("-p",)),
    "exec": _Wrapper(),
    "builtin": _Wrapper(),
    "busybox": _Wrapper(),
}


def _shell_c_index(rest: list[str]) -> int | None:
    """Index of bash/sh's -c flag (possibly bundled: -lc)."""
    for i, t in enumerate(rest):
        if t == "-c" or re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t):
            return i
    return None


def _unwrap(argv: list[str]):
    """Inner command candidates hidden behind a wrapper program (the real
    program of env/nice/timeout/xargs, the -c string of bash/script, the
    applet of busybox, the payload of find -exec ...). Returns [] for a
    plain program, None when the payload cannot be located (unknown
    wrapper flags), and raises _ShellUncertain for an unlexable
    shell-string payload. sudo/doas themselves never get here: they
    refuse as the segment head."""
    if not argv:
        return []
    first = _tok_name(argv[0])
    rest = [str(t) for t in argv[1:]]
    if first in _SHELLS:
        idx = _shell_c_index(rest)
        if idx is None or idx + 1 >= len(rest):
            return []                    # script FILE: contents unknown
        return _lex_shell(rest[idx + 1])
    if first == "find":
        cands: list[list[str]] = []
        i = 1
        while i < len(rest):
            if rest[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                prog = []
                j = i + 1
                while j < len(rest) and rest[j] not in (";", "+", "\\;"):
                    prog.append(rest[j])
                    j += 1
                if prog:
                    cands.append(prog)
                i = j + 1
            else:
                i += 1
        return cands
    spec = _WRAPPERS.get(first)
    if spec is None:
        return []
    cands = []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--":
            i += 1
            break
        if spec.assign and _ASSIGN_RE.match(t):
            i += 1
            continue
        if first == "nice" and re.fullmatch(r"-\d+", t):
            i += 1
            continue
        if t.startswith("-") and t != "-":
            name, eq, val = t.partition("=")
            if name in spec.shell_value:
                frag = val if eq else (rest[i + 1] if i + 1 < len(rest) else "")
                cands.extend(_lex_shell(frag))
                i += 1 if eq else 2
                continue
            if eq or name in spec.values:
                i += 1 if eq else 2
                continue
            if name in spec.bools:
                i += 1
                continue
            return None                  # unknown flag: payload unknown
        break
    pos = rest[i:]
    if spec.needs_shell_value:
        return cands
    if pos:
        cands.append(pos)
        if spec.skip_first and len(pos) > 1:
            cands.append(pos[1:])
    return cands


def _scan_command(argv, cwd: str, home: str, depth: int = 0) -> str | None:
    """The full refusal set over one command, applied RECURSIVELY through
    wrapper programs. A wrapper whose payload cannot be located is
    word-scanned for elevation/session names as a safety net."""
    if not argv:
        return None
    denied = (_deny_elevation(argv) or _deny_session_power(argv)
              or _deny_interactive(argv) or _deny_destroyer(argv, cwd, home))
    if denied:
        return denied
    if depth >= 5:
        return None
    try:
        cands = _unwrap([str(t) for t in argv])
    except _ShellUncertain:
        cands = None
    if cands is None:
        words = re.findall(r"[A-Za-z0-9._/-]+", " ".join(str(t)
                                                         for t in argv))
        for w in words:
            name = _tok_name(w)
            if name in ELEVATE:
                return f"never elevated ({name})"
            if name in _SESSION_IDS or _is_session_name(name):
                return (f"kills or controls the session/compositor "
                        f"({name})")
        return None
    for cand in cands:
        denied = _scan_command(cand, cwd, home, depth + 1)
        if denied:
            return denied
    return None


# ── auto-run hardening for git ─────────────────────────────────
# Auto-run git must not execute REPO-configured programs: external diff
# drivers and textconv filters come from .git/config (clone a hostile
# repo and `git diff` runs their code), and `git status` may poll an
# fsmonitor hook. Read verbs therefore run with these injected.
_GIT_SAFE_CFG = ["-c", "core.fsmonitor=false", "-c",
                 "core.hooksPath=/dev/null"]
_GIT_DIFF_SAFE = ["--no-ext-diff", "--no-textconv"]
_GIT_DIFF_SAFE_VERBS = {"diff", "show"}


def _harden_git_argv(argv: list[str]) -> list[str]:
    """git + read-only hardening flags (see above); non-git argv as-is."""
    if not argv or _tok_name(argv[0]) != "git":
        return list(argv)
    toks = [str(t) for t in argv[1:]]
    sub = ""
    for t in toks:
        if not t.startswith("-"):
            sub = _tok_name(t)
            break
    out = [argv[0], *_GIT_SAFE_CFG, *toks]
    if sub in _GIT_DIFF_SAFE_VERBS or (sub == "log"
                                       and any(t in ("-p", "--patch")
                                               for t in toks)):
        out += _GIT_DIFF_SAFE
    return out


def _auto_rule(argv: list[str], cwd: str, home: str) -> bool:
    """True = this single-argv command is on the read-only allowlist."""
    if not argv:
        return False
    first = _tok_name(argv[0])
    rest = argv[1:]
    if first in _BARE_AUTO:
        return True
    if first in _READ_FILES:
        return _read_files_auto(rest, cwd, home)
    if first == "ip":
        sub = _paths(rest)
        return bool(sub) and sub[0] in ("addr", "link", "route")
    if first == "ss":
        return bool(rest) and all(
            t.startswith("-") and set(t[1:]) <= set("tulnp0Hnx") for t in rest)
    if first == "du":
        for f in _flags(rest):
            if f.startswith("--"):
                return False
            if f[1:] and all(ch in "sh" for ch in f[1:]):
                continue                 # -s -h -sh (combinations)
            if f in ("-d",) or (f.startswith("-d") and len(f) > 2
                                and f[2:].isdigit()):
                continue                 # -d N / -dN (depth)
            return False
        paths = [p for p in _paths(rest) if not p.isdigit()]
        if any(not _under(p, cwd, home, (home, "/tmp")) for p in paths):
            return False
        return True
    if first == "tree":
        if "-L" not in rest:
            return False
        i = rest.index("-L")
        if i + 1 >= len(rest) or not rest[i + 1].isdigit() \
                or int(rest[i + 1]) > 3:
            return False
        return all(_under(p, cwd, home, (home, "/tmp")) for p in _paths(rest)
                   if p != rest[i + 1])
    if first == "find":
        bad = ("-exec", "-execdir", "-delete", "-ok", "-okdir",
               "-fprint", "-fprint0", "-fls", "-fprintf")
        return not any(t in bad or t.startswith("-fprint") for t in rest)
    if first == "journalctl":
        joined = " ".join(rest)
        if any(t.startswith("--vacuum") or t in ("--rotate", "--flush", "--sync")
               for t in rest):
            return False
        if "--user" in rest:
            return True
        for i, t in enumerate(rest):
            if t in ("-n", "--lines") and i + 1 < len(rest):
                n = rest[i + 1]
                return n.isdigit() and int(n) <= 500
            if t.startswith("-n") and t[2:].isdigit():
                return int(t[2:]) <= 500
            if t.startswith("--lines=") and t.split("=", 1)[1].isdigit():
                return int(t.split("=", 1)[1]) <= 500
        return False
    if first == "systemctl":
        user = "--user" in rest
        verbs = _paths(rest)
        verb = verbs[0] if verbs else ""
        if not verb and "--failed" in rest:
            return True            # --failed alone: a read-only unit listing
        allowed = _SYSTEMCTL_READ_USER if user else _SYSTEMCTL_READ_SYS
        return verb in allowed
    if first == "git":
        globals_done = False
        toks = list(rest)
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "-c":
                return False
            if t.startswith("--output"):
                return False
            if t.startswith("-"):
                if t in ("-C", "--git-dir", "--work-tree"):
                    return False       # global overrides: ask
                if t == "--no-pager":
                    i += 1
                    continue
                return False
            break
        sub = toks[i] if i < len(toks) else ""
        if sub not in _GIT_READ:
            return False
        # explicit external diff driver / textconv: the repo's own
        # programs would run — see _harden_git_argv
        if any(t == "--ext-diff" or t.startswith("--ext-diff=")
               or t == "--textconv" or t.startswith("--textconv=")
               for t in toks[i + 1:]):
            return False
        # --output=<file> and -c overrides anywhere: writes/config noise
        return not any(t == "-c" or t.startswith("--output")
                       for t in toks[i + 1:])
    if first == "pacman":
        for t in _flags(rest):
            if t.startswith("-Q"):
                continue
            if t in ("-Ss", "-Si", "-SS", "-Si"):
                continue
            return False
        return bool(_flags(rest))
    if first == "flatpak":
        return _paths(rest)[:1] == ["list"] and all(
            t in ("--app", "--runtime") or t.startswith("--columns")
            for t in _flags(rest))
    if first == "nvidia-smi":
        return not any(t in ("-r", "--reset-gpu") for t in rest)
    if first == "rocm-smi":
        return True
    if first == "radeontop":
        return rest == ["-d", "-", "-l", "1"]
    if first in ("uv", "uvx"):
        return _flags(rest) in (["--version"], ["-V"])
    if first in ("python", "python3", "node"):
        return _flags(rest) == ["--version"]
    if first == "docker":
        toks = _paths(rest)
        return toks[:1] in (["ps"], ["images"]) or toks[:2] == ["compose", "ls"]
    return False


def decide(command: str, cwd=None, timeout=None, home=None, extra_auto=None,
           now=None) -> Decision:
    """Full policy pass over one command string. Never executes anything."""
    home = os.path.expanduser(home or os.environ.get("HOME") or "~")
    raw = str(command or "").strip()
    if not raw:
        return Decision(command=raw, mode="refuse", reason="empty command")
    try:
        raw_timeout = float(timeout if timeout not in (None, "")
                            else DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return Decision(command=raw, mode="refuse",
                        reason=f"timeout must be a number of seconds, "
                               f"got {timeout!r}")
    tmo = min(max(raw_timeout, 0.5), MAX_TIMEOUT)
    try:
        argv_all = _split(raw)
    except ValueError as e:
        return Decision(command=raw, mode="refuse", reason=str(e))

    # working directory
    cwd_tok = str(cwd or "").strip() or home
    cwd_abs = expand(cwd_tok, home)
    if not cwd_abs.startswith("/"):
        cwd_abs = os.path.abspath(cwd_abs)
    exists = os.path.isdir(cwd_abs)
    secret = any(touches_secret(t, home) for t in argv_all) \
        or touches_secret(cwd_tok, home)

    shell = has_shell_syntax(raw)
    too_complex = ("it is too complex to check safely — run it yourself")
    try:
        segs = _lex_shell(raw) if shell else [argv_all]
    except _ShellUncertain:
        return Decision(command=raw, mode="refuse", reason=too_complex,
                        cwd=cwd_abs, exists=exists, timeout=tmo,
                        secret_touch=secret, shell=shell)
    # The refusal set sees EVERY segment and looks THROUGH wrapper
    # programs and shell strings (rule 1: refusals hold even with
    # approval, so `env sudo ls` or `bash -c "ls; rm -rf ~"` must not
    # sneak in as a mere ask).
    for seg in segs:
        denied = _scan_command(seg, cwd_abs, home)
        if denied:
            return Decision(command=raw, mode="refuse", reason=denied,
                            cwd=cwd_abs, exists=exists, timeout=tmo,
                            secret_touch=secret, shell=shell)
    argv = ["bash", "-c", raw] if shell else argv_all

    base = Decision(command=raw, argv=argv, shell=shell, cwd=cwd_abs,
                    exists=exists, timeout=tmo, secret_touch=secret)

    if shell:
        base.mode = "ask"
        base.reason = "it uses shell syntax (it runs through bash -c, exactly " \
                      "as written)"
        return base
    if not exists:
        base.mode = "ask"
        base.reason = f"the working directory {cwd_abs} does not exist"
        return base
    in_home = cwd_abs == home or cwd_abs.startswith(home + "/")
    in_tmp = cwd_abs == "/tmp" or cwd_abs.startswith("/tmp/")
    if not (in_home or in_tmp):
        base.mode = "ask"
        base.reason = f"the working directory {cwd_abs} is outside $HOME and " \
                      "/tmp"
        return base
    if secret:
        base.mode = "ask"
        base.reason = "the arguments touch a secret path"
        return base
    extra = {str(x).strip().lower() for x in (extra_auto or []) if str(x).strip()}
    prog = _tok_name(argv[0])
    two = prog + " " + _tok_name(argv[1]) if len(argv) > 1 else ""
    if prog in extra or (two and two in extra):
        base.mode = "auto"
        base.argv = _harden_git_argv(argv)
        return base
    if _auto_rule(argv, cwd_abs, home):
        base.mode = "auto"
        base.argv = _harden_git_argv(argv)
        return base
    base.mode = "ask"
    base.reason = f"'{prog}' is not on the read-only auto-run allowlist"
    return base
