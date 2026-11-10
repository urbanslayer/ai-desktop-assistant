"""Running one scrubbed argv + sanitising its output (docs/COMMANDS.md
rules 2-5, 8). Tests inject their own runner; the real path below is the
only place subprocess is touched, and tests/conftest.py hard-fails it for
this package unless a test opts into a fixture binary under a tmp dir.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys

# ── environment scrub (rule 3) ─────────────────────────────────
# The desktop process loads the checkout's .env (the hub key lives there);
# none of that may leak into a command the model chose.
_SECRET_ENV_MATCH = re.compile(r"(.*_KEY|.*_TOKEN|.*_SECRET|.*PASSWORD.*)$",
                               re.I)
_SECRET_ENV_PREFIX = ("RIKO_", "ASR_", "LLM_", "FISH_", "GROQ_", "OPENAI_",
                      "ANTHROPIC_", "HF_", "AWS_")
_SECRET_ENV_EXACT = {"GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK",
                     "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS"}

_CLEAN_ENV = {"LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
              "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": ""}


def env_is_secret(name: str) -> bool:
    n = str(name or "")
    up = n.upper()
    return (bool(_SECRET_ENV_MATCH.match(up)) or up.startswith(_SECRET_ENV_PREFIX)
            or up in _SECRET_ENV_EXACT)


def scrub_env(env) -> dict:
    """User env minus secrets, plus a non-interactive, unpaginated base."""
    out = {k: v for k, v in (env or {}).items()
           if not env_is_secret(k) and k != "_"}
    out.update(_CLEAN_ENV)
    # keep the binary path so `git`/`uv` still resolve
    for keep in ("PATH", "HOME", "USER", "SHELL", "XDG_RUNTIME_DIR",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LD_LIBRARY_PATH"):
        if keep in (env or {}):
            out[keep] = env[keep]
    return out


# ── output cap + redaction (rules 5, 8) ────────────────────────
MAX_STREAM_CHARS = 8000
TRUNC_MARK = "\n…[truncated]…\n"

_REDACTIONS = (
    # PEM private-key blocks first (multi-line, swallow the whole body)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
                r"-----END [A-Z ]*PRIVATE KEY-----"), "[redacted]"),
    (re.compile(r"(?i)\bauthorization:\s*bearer\s+\S+"),
     "Authorization: Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
     "[redacted]"),                                   # JWT
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}"), "[redacted]"),
)


def redact(text: str) -> str:
    out = str(text or "")
    for rx, repl in _REDACTIONS:
        out = rx.sub(repl, out)
    return out


def cap_stream(text: str) -> tuple[str, bool]:
    """Head + tail with an explicit marker when over the cap."""
    text = str(text or "")
    if len(text) <= MAX_STREAM_CHARS:
        return text, False
    keep = (MAX_STREAM_CHARS - len(TRUNC_MARK)) // 2
    return text[:keep] + TRUNC_MARK + text[-keep:], True


# ── the real runner ────────────────────────────────────────────
def run_argv(argv, cwd, env, timeout, on_process=None):
    """Execute argv (shell=False; bash -c for shell mode) with stdin
    dev-null and no TTY; kill the whole process group on timeout.
    Returns {exit_code, stdout, stderr, timed_out, cancelled}."""
    argv = list(argv)
    shell_mode = len(argv) >= 2 and os.path.basename(argv[0]) == "bash" \
        and argv[1] == "-c"
    nullin = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=nullin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", start_new_session=True)
    except FileNotFoundError:
        prog = argv[0] if not shell_mode else (argv[2].split() or ["?"])[0]
        return {"exit_code": 127, "stdout": "",
                "stderr": f"command not found: {prog}", "timed_out": False,
                "cancelled": False}
    except OSError as e:
        return {"exit_code": 126, "stdout": "", "stderr": str(e),
                "timed_out": False, "cancelled": False}
    if on_process is not None:
        on_process(proc)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
    cancelled = bool(getattr(proc, "_riko_cancelled", False))
    code = proc.returncode if proc.returncode is not None else -9
    if sys.platform != "win32" and code is not None and code < 0:
        code = 128 - code            # signal: report like a shell does
    return {"exit_code": code, "stdout": out or "", "stderr": err or "",
            "timed_out": timed_out, "cancelled": cancelled}


def kill_group(proc, reason="cancelled"):
    setattr(proc, "_riko_cancelled", True)
    _kill_group(proc)


def _kill_group(proc):
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
