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
  * never interactive (vim/less/top/ssh-without-command/REPL/man …);
  * no shell by default: shell syntax forces approval + `bash -c`;
  * auto-run only for allowlisted read-only programs over clean arguments
    (no secret path, no write flag, cwd inside $HOME or /tmp).
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


def _segments(argv: list[str]) -> list[list[str]]:
    """Split a shell-mode argv into per-command segments on | ; & so the
    hard-refusal checks see `sudo` even inside a pipeline."""
    segs, cur = [], []
    for tok in argv:
        if tok in ("|", ";", "&&", "||", "&"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


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
    if shell:
        segs = _segments(argv_all)
        for seg in segs:
            prog = _tok_name(seg[0]) if seg else ""
            if prog in ELEVATE or (prog == "machinectl"
                                   and "shell" in _paths(seg[1:])):
                return Decision(command=raw, mode="refuse",
                                reason=f"never elevated ({prog})", cwd=cwd_abs,
                                exists=exists, timeout=tmo, secret_touch=secret,
                                shell=True)
            denied = (_deny_session_power(seg) or _deny_interactive(seg)
                      or _deny_destroyer(seg, cwd_abs, home))
            if denied:
                return Decision(command=raw, mode="refuse", reason=denied,
                                cwd=cwd_abs, exists=exists, timeout=tmo,
                                secret_touch=secret, shell=True)
        argv = ["bash", "-c", raw]
    else:
        denied = (_deny_session_power(argv_all) or _deny_interactive(argv_all)
                  or _deny_destroyer(argv_all, cwd_abs, home))
        if denied:
            return Decision(command=raw, mode="refuse", reason=denied,
                            cwd=cwd_abs, exists=exists, timeout=tmo,
                            secret_touch=secret)
        if argv_all and (_tok_name(argv_all[0]) in ELEVATE
                         or (_tok_name(argv_all[0]) == "machinectl"
                             and "shell" in _paths(argv_all[1:]))):
            return Decision(command=raw, mode="refuse",
                            reason=f"never elevated "
                                   f"({_tok_name(argv_all[0])})",
                            cwd=cwd_abs, exists=exists, timeout=tmo,
                            secret_touch=secret)
        argv = argv_all

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
        return base
    if _auto_rule(argv, cwd_abs, home):
        base.mode = "auto"
        return base
    base.mode = "ask"
    base.reason = f"'{prog}' is not on the read-only auto-run allowlist"
    return base
