"""CommandService: the run_command executor (docs/COMMANDS.md).

Approval design (same one-time-token trick as the terminal run keys in
riko/typing/engine.py): anything that is not allowlisted raises
client_tools.NeedConfirmation carrying a `cmd_token` this service issued
itself, bound to the EXACT command string + resolved cwd with a short
TTL. The runtime asks the owner once, showing the exact command and
directory, and re-calls with confirmed=True + the token; the token is
burned on use. A generic hub approval only injects confirmed=True
(riko/client_tools.py) and can NEVER substitute for that ask.

One command at a time (lock); the tray's 'Stop typing / command' calls
cancel(), which SIGKILLs the whole process group. Every call gets one
audit line: `[cmd] auto|approved|refused <command> (<cwd>) exit=N 0.3s`
(denials are logged by install()'s ask_approval wrapper, because the
runtime never re-calls the executor after a deny). The command text is
logged; the output never is.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from ..client_tools import NeedConfirmation, ToolError
from . import policy, runner
from .recipes import DEFAULT_RECIPES

ASK_PREFIX = "run command:"
CMD_TOKEN_TTL = 120.0    # s: an approval bubble that stale is worthless


class CommandService:
    """Policy + execution for run_command. `runner` is the injectable
    exec hook (tests inject fakes; nothing here runs a real command
    without one), `env` the parent environment to scrub (default
    os.environ), `printer` the audit sink."""

    def __init__(self, cfg=None, home=None, runner_fn=None, env=None,
                 printer=print, now=None, recipes=None):
        c = cfg if isinstance(cfg, dict) else {}
        if isinstance(c.get("commands"), dict):
            c = c["commands"]            # accept the whole config too
        self.home = os.path.expanduser(home or os.environ.get("HOME") or "~")
        self.extra_auto = [str(x) for x in (c.get("auto_allow") or [])]
        self.recipes = ([dict(r) for r in DEFAULT_RECIPES] if recipes is None
                        else recipes)
        self._runner = runner_fn or runner.run_argv
        self._env_source = env if env is not None else os.environ
        self._print = printer
        self._now = now or time.monotonic
        self._lock = threading.Lock()      # one command at a time (rule 10)
        self._tokens: dict[str, dict] = {}
        self._proc = None                  # live Popen (cancel target)
        self.last_exec = None              # last argv/env/timeout (tests)

    # ── one-time approval token (rules 1 + Shape) ──────────────
    def _issue_token(self, command: str, cwd: str) -> str:
        tok = secrets.token_urlsafe(16)
        now = self._now()
        self._tokens = {t: r for t, r in self._tokens.items()
                        if r["expires"] > now}
        self._tokens[tok] = {"cmd": command, "cwd": cwd,
                             "expires": now + CMD_TOKEN_TTL}
        return tok

    def _consume_token(self, token, command: str, cwd: str) -> bool:
        """Validate and burn. False = no grant (expired, wrong command or
        directory, already spent, or minted by something other than our
        own NeedConfirmation round trip)."""
        if not token:
            return False
        rec = self._tokens.pop(str(token), None)
        if rec is None or self._now() > rec["expires"]:
            return False
        return rec["cmd"] == command and rec["cwd"] == cwd

    # ── audit (rule 9) ─────────────────────────────────────────
    def _audit(self, kind, command, cwd, exit_code=None, dur=None):
        tail = (f"exit={exit_code} {dur:.1f}s" if dur is not None
                else f"exit={'-' if exit_code is None else exit_code} -")
        self._print(f"[cmd] {kind} {command} ({cwd}) {tail}")

    # ── cancellation (rule 10) ─────────────────────────────────
    def cancel(self):
        """Tray 'Stop typing / command': kill the running command's whole
        process group. Safe to call when nothing runs."""
        proc = self._proc
        if proc is not None:
            runner.kill_group(proc)
            return True
        return False

    # ── the executor ───────────────────────────────────────────
    def run(self, command=None, cwd=None, timeout=None, confirmed=False,
            cmd_token=None, **_kw):
        raw = str(command or "").strip()
        if not raw:
            raise ToolError("run_command: nothing to run")
        d = policy.decide(raw, cwd=cwd, timeout=timeout, home=self.home,
                          extra_auto=self.extra_auto)
        if d.mode == "refuse":
            self._audit("refused", raw, d.cwd or str(cwd or self.home))
            raise ToolError(f"I can't run that: {d.reason}.")
        granted = self._consume_token(cmd_token, d.command, d.cwd)
        if d.mode != "auto" and not granted:
            params = {"command": d.command, "cwd": d.cwd,
                      "timeout": d.timeout,
                      "cmd_token": self._issue_token(d.command, d.cwd)}
            summary = f"{ASK_PREFIX} `{d.command}` in {d.cwd}"
            if d.shell:
                summary += " (shell syntax — runs through bash -c)"
            if d.secret_touch:
                summary += " — this may expose secrets to the model"
            raise NeedConfirmation(summary, "run_command", params)

        if not self._lock.acquire(blocking=False):
            raise ToolError("another command is still running — try again "
                            "in a moment")
        try:
            return self._exec(d, auto=(d.mode == "auto"))
        finally:
            self._lock.release()

    def _exec(self, d, auto):
        env = runner.scrub_env(self._env_source)
        self.last_exec = {"argv": list(d.argv), "cwd": d.cwd,
                          "timeout": d.timeout, "env": dict(env)}
        t0 = self._now()

        def _note(proc):
            self._proc = proc

        try:
            out = self._runner(list(d.argv), d.cwd, env, d.timeout,
                               on_process=_note)
        finally:
            self._proc = None
        dur = self._now() - t0
        stdout, cut1 = runner.cap_stream(runner.redact(out.get("stdout") or ""))
        stderr, cut2 = runner.cap_stream(runner.redact(out.get("stderr") or ""))
        code = out.get("exit_code")
        result = {"exit_code": code, "stdout": stdout, "stderr": stderr,
                  "truncated": bool(cut1 or cut2),
                  "duration_ms": int(dur * 1000), "cwd": d.cwd,
                  "auto": bool(auto)}
        if out.get("timed_out"):
            result["timed_out"] = True
            result["note"] = (f"timed out after {d.timeout:g} s — the whole "
                              f"process group was killed")
        if out.get("cancelled"):
            result["cancelled"] = True
            result["note"] = "cancelled by the owner (Stop typing / command)"
        self._audit("auto" if auto else "approved", d.command, d.cwd,
                    code, dur)
        return result
