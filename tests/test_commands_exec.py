"""Service + runner tests for run_command (docs/COMMANDS.md rules 3-10):
env scrubbing, output cap, redaction, timeout process-group kill,
cancellation, one-time approval token, audit lines.

Fake runners everywhere; the ONE real exec (the conftest opt-in fixture
script + the timeout kill) runs a tmp shell script, never a model-chosen
program — and a companion test proves the conftest guard blocks the real
path for this package by default.
"""
import os
import stat
import threading
import time

import pytest

from riko.client_tools import NeedConfirmation, ToolError
from riko.commands import CommandService
from riko.commands import runner as rmod


class FakeRunner:
    """Records every exec; returns a canned result. `busy` lets a test
    simulate a long command while a second call tries to sneak in."""

    def __init__(self, stdout="ok", stderr="", exit_code=0, timed_out=False):
        self.calls = []
        self.result = {"exit_code": exit_code, "stdout": stdout,
                       "stderr": stderr, "timed_out": timed_out,
                       "cancelled": False}
        self.hook = None

    def __call__(self, argv, cwd, env, timeout, on_process=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env),
                           "timeout": timeout})
        if self.hook is not None:
            self.hook(on_process)
        return dict(self.result)


class FakeProc:
    pid = 999_999

    def __init__(self):
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


def svc(home, runner_fn=None, cfg=None, env=None, log=None, now=None):
    return CommandService(cfg=cfg or {}, home=home, runner_fn=runner_fn,
                          env=env if env is not None else {"PATH": "/bin"},
                          printer=(log.append if log is not None
                                   else (lambda *a: None)),
                          now=now)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "notes").mkdir(parents=True)
    return str(h)


# ── auto-run happy path ────────────────────────────────────────
def test_auto_runs_without_asking(home):
    r = FakeRunner(stdout="/dev/sda 42G")
    s = svc(home, r)
    out = s.run(command="df -h")
    assert out["auto"] is True and out["exit_code"] == 0
    assert out["stdout"] == "/dev/sda 42G" and out["truncated"] is False
    assert out["cwd"] == os.path.expanduser(home)
    assert out["duration_ms"] >= 0
    assert r.calls[0]["argv"] == ["df", "-h"]


def test_shell_mode_runs_bash_c(home):
    r = FakeRunner()
    s = svc(home, r)
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="ls | grep x")
    p = ei.value.params
    out = s.run(confirmed=True, **p)
    assert out["auto"] is False
    assert r.calls[-1]["argv"] == ["bash", "-c", "ls | grep x"]


# ── one-time approval token ────────────────────────────────────
def test_needconfirmation_shows_exact_command_and_cwd(home):
    s = svc(home, FakeRunner())
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="echo hi")
    reason = ei.value.reason
    assert "run command:" in reason
    assert "echo hi" in reason and home in reason
    assert ei.value.method == "run_command"
    assert ei.value.params["command"] == "echo hi"
    assert ei.value.params["cmd_token"]


def test_token_round_trip_grants_once(home):
    r = FakeRunner()
    s = svc(home, r)
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="echo hi")
    p = ei.value.params
    out = s.run(confirmed=True, **p)
    assert out["auto"] is False and r.calls[0]["argv"] == ["echo", "hi"]
    # one-time: spending the same token again must NOT run
    with pytest.raises(NeedConfirmation):
        s.run(confirmed=True, **p)
    assert len(r.calls) == 1


def test_generic_hub_confirmation_cannot_grant(home):
    """confirmed=True WITHOUT our token (the hub-injected kind): still asks."""
    r = FakeRunner()
    s = svc(home, r)
    with pytest.raises(NeedConfirmation):
        s.run(command="echo hi", confirmed=True)
    assert r.calls == []


def test_token_is_bound_to_command_and_cwd(home, tmp_path):
    s = svc(home, FakeRunner())
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="echo hi")
    p = ei.value.params
    # same token, different command text -> no grant
    with pytest.raises(NeedConfirmation):
        s.run(confirmed=True, **{**p, "command": "echo rm -rf /"})
    with pytest.raises(NeedConfirmation) as ei2:
        s.run(command="echo hi", cwd=str(tmp_path))
    p2 = ei2.value.params
    with pytest.raises(NeedConfirmation):
        s.run(confirmed=True, **{**p2, "cwd": os.path.expanduser(home)})


def test_token_expires(home):
    clock = {"t": 1000.0}
    r = FakeRunner()
    s = svc(home, r, now=lambda: clock["t"])
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="echo hi")
    p = ei.value.params
    clock["t"] += 121.0
    with pytest.raises(NeedConfirmation):
        s.run(confirmed=True, **p)
    assert r.calls == []


# ── secret path approval warning ───────────────────────────────
def test_secret_ask_warns(home):
    s = svc(home, FakeRunner())
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="ls ~/.ssh")
    assert "secrets" in ei.value.reason


# ── refusals ───────────────────────────────────────────────────
def test_refusal_raises_toolerror_and_audits(home):
    log = []
    r = FakeRunner()
    s = svc(home, r, log=log)
    with pytest.raises(ToolError, match="sudo"):
        s.run(command="sudo ls", confirmed=True)   # approval changes nothing
    assert r.calls == []
    assert log and log[0].startswith("[cmd] refused sudo ls")


# ── env scrub (rule 3) ─────────────────────────────────────────
def test_env_scrubbed(home):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/user", "USER": "user",
           "SOMEAPP_API_KEY": "k", "MY_TOKEN": "t", "APP_SECRET": "s",
           "DB_PASSWORD": "p", "RIKO_HUB_KEY": "h", "ASR_API_KEY": "a",
           "LLM_API_KEY": "l", "FISH_API_KEY": "f", "GROQ_API_KEY": "g",
           "OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "n", "HF_TOKEN": "x",
           "GITHUB_TOKEN": "gh", "GH_TOKEN": "gh", "AWS_ACCESS_KEY_ID": "aw",
           "SSH_AUTH_SOCK": "/s", "GPG_AGENT_INFO": "g",
           "DBUS_SESSION_BUS_ADDRESS": "b", "SHELL": "/bin/zsh",
           "EDITOR": "vim"}
    r = FakeRunner()
    s = svc(home, r, env=env)
    s.run(command="whoami")
    child = r.calls[0]["env"]
    for gone in ("SOMEAPP_API_KEY", "MY_TOKEN", "APP_SECRET", "DB_PASSWORD",
                 "RIKO_HUB_KEY", "ASR_API_KEY", "LLM_API_KEY", "FISH_API_KEY",
                 "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                 "HF_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "AWS_ACCESS_KEY_ID",
                 "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS"):
        assert gone not in child, gone
    assert child["PATH"] == "/usr/bin:/bin" and child["HOME"] == "/home/user"
    assert child["EDITOR"] == "vim"
    assert child["LANG"] == "C.UTF-8" and child["TERM"] == "dumb"
    assert child["NO_COLOR"] == "1" and child["PAGER"] == "cat"
    assert child["GIT_PAGER"] == "cat" and child["SYSTEMD_PAGER"] == ""


# ── output cap + redaction (rules 5, 8) ────────────────────────
def test_output_cap(head="x" * 50_000):
    text, cut = rmod.cap_stream(head)
    assert cut is True and len(text) <= rmod.MAX_STREAM_CHARS
    assert "…[truncated]…" in text
    assert text.startswith("xxxx") and text.endswith("xxxx")
    small, cut2 = rmod.cap_stream("short")
    assert small == "short" and cut2 is False


def test_service_marks_truncated(home):
    s = svc(home, FakeRunner(stdout="y" * 50_000))
    out = s.run(command="ls")
    assert out["truncated"] is True
    assert len(out["stdout"]) <= rmod.MAX_STREAM_CHARS


@pytest.mark.parametrize("secret,gone", [
    ("sk-abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrst"),
    ("ghp_" + "A" * 36, "AAAAAAA"),
    ("hf_" + "B" * 34, "BBBBBBB"),
    ("xox" + "b-123456789012-abcdefghijklmno", "abcdefghijklmno"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
     "eyJzdWIiOiIxMjM0"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnZzaXMAAAAJ\n"
     "-----END OPENSSH PRIVATE KEY-----", "b3BlbnZzaXM"),
    ("Authorization: Bearer abc123secret456", "abc123secret456"),
])
def test_redaction(secret, gone):
    out = rmod.redact(f"banner\nvalue: {secret}\nend")
    assert "[redacted]" in out
    assert gone not in out
    assert "banner" in out and "end" in out        # benign lines survive


def test_service_redacts_output(home):
    s = svc(home, FakeRunner(stdout="token sk-abcdefghijklmnopqrstuvwx ok"))
    out = s.run(command="whoami")
    assert "sk-abcdefghijklmnopqrstuvwx" not in out["stdout"]
    assert "[redacted]" in out["stdout"]


# ── timeout (rule 5) — REAL exec of a tmp fixture script ──────
def test_timeout_kills_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_COMMANDS_ALLOW_REAL", str(tmp_path))
    sh = tmp_path / "stubborn.sh"
    sh.write_text("#!/bin/sh\ntrap '' INT TERM\nsleep 30\n")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
    t0 = time.monotonic()
    out = rmod.run_argv([str(sh)], str(tmp_path), {}, 0.5)
    dur = time.monotonic() - t0
    assert out["timed_out"] is True
    assert dur < 10, f"process group survived the kill ({dur:.1f}s)"
    assert out["exit_code"] == 137          # SIGKILL (128+9)


def test_real_runner_executes_and_scrubs(tmp_path, monkeypatch):
    """conftest opt-in: a harmless fixture script MAY run for real."""
    monkeypatch.setenv("RIKO_COMMANDS_ALLOW_REAL", str(tmp_path))
    sh = tmp_path / "echoenv.sh"
    sh.write_text("#!/bin/sh\necho \"k=$SOMEAPP_API_KEY lang=$LANG\"\n")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
    out = rmod.run_argv([str(sh)], str(tmp_path),
                        rmod.scrub_env({"SOMEAPP_API_KEY": "leak",
                                        "PATH": "/usr/bin:/bin"}), 10)
    assert out["exit_code"] == 0 and out["timed_out"] is False
    assert out["stdout"].strip() == "k= lang=C.UTF-8"


def test_conftest_guard_blocks_real_commands_by_default(tmp_path):
    """Without the opt-in, the real subprocess path raises for this
    package — a forgotten fake runner fails loudly (docs/COMMANDS.md)."""
    assert "RIKO_COMMANDS_ALLOW_REAL" not in os.environ
    with pytest.raises(RuntimeError, match="must not run real commands"):
        rmod.run_argv(["true"], str(tmp_path), {}, 5)


# ── cancellation + one-at-a-time (rule 10) ─────────────────────
def test_cancel_kills_process_group(home):
    s = svc(home, FakeRunner())
    assert s.cancel() is False               # nothing running
    proc = FakeProc()
    s._proc = proc
    assert s.cancel() is True
    assert proc.killed and getattr(proc, "_riko_cancelled", False)


def test_cancelled_result_flagged(home):
    holder = {}
    s = None

    def runner(argv, cwd, env, timeout, on_process=None):
        p = FakeProc()
        if on_process:
            on_process(p)
        holder["s"].cancel()                  # like the tray click mid-run
        return {"exit_code": -9, "stdout": "", "stderr": "",
                "timed_out": False,
                "cancelled": bool(getattr(p, "_riko_cancelled", False))}

    s = svc(home, runner)
    holder["s"] = s
    out = s.run(command="ls")
    assert out["cancelled"] is True and "cancelled" in out["note"]


def test_one_command_at_a_time(home):
    start = threading.Event()
    release = threading.Event()

    def runner(argv, cwd, env, timeout, on_process=None):
        start.set()
        assert release.wait(5)
        return {"exit_code": 0, "stdout": "", "stderr": "",
                "timed_out": False, "cancelled": False}

    s = svc(home, runner)
    t = threading.Thread(target=lambda: s.run(command="ls"))
    t.start()
    assert start.wait(5)
    with pytest.raises(ToolError, match="another command"):
        s.run(command="ls")                   # second call must not queue in
    release.set()
    t.join(5)
    assert not t.is_alive()


# ── timeout surfaced in the result ─────────────────────────────
def test_timed_out_note(home):
    s = svc(home, FakeRunner(timed_out=True, stderr="partial"))
    out = s.run(command="ls")
    assert out["timed_out"] is True and "process group was killed" in out["note"]


# ── audit lines (rule 9) ───────────────────────────────────────
def test_audit_lines(home):
    log = []
    s = svc(home, FakeRunner(stdout="out"), log=log)
    s.run(command="ls")
    assert log[0].startswith("[cmd] auto ls (") and "exit=0" in log[0]
    with pytest.raises(NeedConfirmation) as ei:
        s.run(command="echo hi")
    s.run(confirmed=True, **ei.value.params)
    assert log[1].startswith("[cmd] approved echo hi (")
    assert "out" not in "\n".join(log)        # output never logged
    with pytest.raises(ToolError):
        s.run(command="sudo ls")
    assert log[2].startswith("[cmd] refused sudo ls")
