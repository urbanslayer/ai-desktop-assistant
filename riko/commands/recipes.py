"""Natural-language recipe catalogue for run_command (docs/COMMANDS.md
'Recipes').

A recipe maps a short natural phrasing the owner might use ("how much
space is free on the root volume?") to the exact command that answers it
(`df -h /`). Defaults live here; the owner overrides/extends them in
character_config.yaml under `commands.recipes` (a list of {ask, command,
note?}; an entry with the same `ask` replaces the default, new asks are
appended; `recipes: false` clears them).

Honesty rule (validated by `validate()` and a test): every default
recipe's command either auto-runs under the policy allowlist as written,
or carries `asks: true` — meaning running it pops the approval bubble
(shell pipes, templated placeholders). Riko never promises a command
that would secretly ask.

`lookup()` is a dumb keyword scorer on purpose: she asks in plain words,
gets a ranked shortlist, then calls run_command with the recipe's
command — the policy still decides auto/ask/refuse at run time.
"""
from __future__ import annotations

import re

# ── defaults ────────────────────────────────────────────────────
# Keep compact: `ask` is a phrasing, `command` runs as-is, `note` is for
# her (never shown to the owner). `asks: True` marks a command that will
# hit the approval bubble (pipe/glob); `templated: True` means the
# command has <placeholders> she must fill first.
DEFAULT_RECIPES: tuple[dict, ...] = (
    {"ask": "how much space is free on the root volume",
     "command": "df -h /"},
    {"ask": "what size is the home volume", "command": "df -h /home"},
    {"ask": "how much of my home folder is used", "command": "du -sh ~"},
    {"ask": "memory and swap totals", "command": "free -h"},
    {"ask": "which folders eat the most space in my home",
     "command": "du -h -d1 ~ | sort -h", "asks": True,
     "note": "pipe -> approval; last rows are the biggest"},
    {"ask": "space on every real disk",
     "command": "df -h -x tmpfs -x devtmpfs"},
    {"ask": "what's using my memory",
     "command": "ps -eo pid,comm,%mem,%cpu --sort=-%mem",
     "note": "top rows = biggest users; long lists cap automatically "
             "(free -h answers memory/swap totals)"},
    {"ask": "what's using my CPU",
     "command": "ps -eo pid,comm,%cpu,%mem --sort=-%cpu"},
    {"ask": "uptime and load average", "command": "uptime"},
    {"ask": "CPU model and cores", "command": "lscpu"},
    {"ask": "CPU temperatures", "command": "sensors"},
    {"ask": "is my GPU hot", "command": "rocm-smi",
     "note": "owner GPU is AMD RX 9060 XT; nvidia-smi recipe only if "
             "an NVIDIA card is ever present"},
    {"ask": "GPU utilisation over a second", "command": "radeontop -d - -l 1"},
    {"ask": "GPU status (NVIDIA)", "command": "nvidia-smi"},
    {"ask": "disks and partitions", "command": "lsblk -f"},
    {"ask": "mounted filesystems", "command": "findmnt -D"},
    {"ask": "my IP addresses", "command": "ip -br addr"},
    {"ask": "which ports are listening", "command": "ss -tuln"},
    {"ask": "recent errors from my user services",
     "command": "journalctl --user -p err -n 50"},
    {"ask": "last system log lines", "command": "journalctl -n 50"},
    {"ask": "failed system services", "command": "systemctl --failed"},
    {"ask": "failed user services", "command": "systemctl --user --failed"},
    {"ask": "kernel version", "command": "uname -r"},
    {"ask": "which distro/version am I on", "command": "cat /etc/os-release"},
    {"ask": "version of an installed package", "command": "pacman -Qi <pkg>",
     "asks": True, "templated": True,
     "note": "replace <pkg> with the package name, then it auto-runs "
             "(pacman -Q… is read-only)"},
    {"ask": "search installed/available packages",
     "command": "pacman -Ss <term>", "asks": True, "templated": True},
    {"ask": "pending updates", "command": "checkupdates",
     "note": "read-only; empty output = up to date"},
    {"ask": "git status of a project", "command": "git status",
     "note": "pass cwd=<project directory>"},
    {"ask": "running docker containers", "command": "docker ps"},
    {"ask": "docker images on this machine", "command": "docker images"},
    {"ask": "docker compose projects", "command": "docker compose ls",
     "note": "pass cwd=<project directory> for the right compose file"},
)

DESC_BUDGET = 900            # max chars for the 'common asks -> command' block
_STOP = {"what", "whats", "is", "the", "a", "an", "of", "my", "i",
         "on", "in", "to", "for", "how", "much", "are", "does", "do", "am",
         "me", "please", "show", "tell", "give", "check", "and", "its",
         "this", "that", "with", "from", "get", "list", "current", "you",
         "cant"}


def compact_desc(recipes, budget: int = DESC_BUDGET) -> str:
    """The 'common asks -> command' block for the run_command tool
    description: as many defaults as fit the budget, catalogue order."""
    parts, used = [], 0
    for r in recipes:
        if r.get("templated"):
            continue
        piece = f"{r['ask']} -> {r['command']}"
        if used + len(piece) + 2 > budget:
            break
        parts.append(piece)
        used += len(piece) + 2
    return "; ".join(parts)


def merge_recipes(block) -> list[dict]:
    """defaults + the owner's `commands.recipes` list (override by `ask`,
    append new; `recipes: false` clears everything)."""
    conf = (block or {}).get("recipes") if isinstance(block, dict) else None
    if conf is False:
        return []
    merged: dict[str, dict] = {}
    for r in DEFAULT_RECIPES:
        merged[r["ask"]] = dict(r)
    if isinstance(conf, list):
        for entry in conf:
            if not isinstance(entry, dict):
                continue
            ask = str(entry.get("ask") or "").strip()
            cmd = str(entry.get("command") or "").strip()
            if not ask or not cmd:
                continue
            clean = {"ask": ask, "command": cmd}
            for k in ("note", "asks", "templated"):
                if entry.get(k) is not None:
                    clean[k] = entry[k]
            merged[ask] = clean          # same ask -> override; new -> append
    return list(merged.values())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower())
            if len(w) > 1 and w not in _STOP}


def score(query: str, entry: dict) -> int:
    q = _tokens(query)
    if not q:
        return 0
    a = _tokens(entry.get("ask", ""))
    c = _tokens(entry.get("command", ""))
    return 3 * len(q & a) + len(q & c)


def lookup(query: str, recipes: list[dict], limit: int = 6) -> list[dict]:
    """Ranked matches (score>0). Empty/None query returns everything."""
    if not str(query or "").strip():
        return [dict(r) for r in recipes]
    scored = [(score(query, r), i, r) for i, r in enumerate(recipes)]
    hits = [(s, i, r) for s, i, r in scored if s > 0]
    hits.sort(key=lambda t: (-t[0], t[1]))
    return [dict(r) for _, _, r in hits[:limit]]


def validate(recipes: list[dict] | None = None, home=None) -> list[str]:
    """Every recipe auto-runs as written OR is marked asks:true;
    templated recipes must be asks:true and carry a <placeholder>.
    Returns a list of problems ([] = the catalogue is honest)."""
    from . import policy
    problems = []
    for r in (recipes if recipes is not None else DEFAULT_RECIPES):
        ask = str(r.get("ask") or "")
        cmd = str(r.get("command") or "")
        if not ask or not cmd:
            problems.append(f"recipe needs both ask and command: {r!r}")
            continue
        templated = bool(r.get("templated"))
        if templated and not re.search(r"<[^>]+>", cmd):
            problems.append(f"templated recipe without <placeholder>: {cmd}")
        if r.get("asks"):
            continue
        if templated:
            problems.append(f"templated recipe must set asks: true: {cmd}")
            continue
        d = policy.decide(cmd, home=home)
        if d.mode != "auto":
            problems.append(f"{cmd!r} ({ask!r}) is not auto-run "
                            f"(mode={d.mode}{', ' + d.reason if d.reason else ''}"
                            f") — run it through policy or mark asks: true")
    return problems


__all__ = ["DEFAULT_RECIPES", "compact_desc", "lookup", "merge_recipes",
           "score", "validate"]
