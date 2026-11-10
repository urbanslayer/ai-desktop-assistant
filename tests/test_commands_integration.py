"""End-to-end integration for run_command (docs/COMMANDS.md 'Shape' +
'Tests'): driven through ClientRuntime._dispatch_request with a fake
owner-approval answer, exactly like the app-editing pack test does.

- allowlisted read-only command: returns output WITHOUT any ask;
- non-allowlisted: asks exactly once, showing command + cwd; approve runs
  once, deny runs nothing;
- a generic hub approval (needs_approval=True -> injected confirmed=True)
  is NOT enough: our own ask still fires and its token alone grants;
- refusals never reach the runner;
- the pack registers through client_tools.register_extra (rides the
  awareness gate), install() wires the 'denied' audit line, and the hub
  prompt gains its one line only when run_command is offered.
Fake runners + fake home only: the conftest real-command guard holds.
"""
import yaml
from pathlib import Path

import pytest

from hub.tools.registry import tools_prompt
from riko import client_tools as ct
from riko import commands as commands_pack

SECRET = "RIKO_SECRET_FOR_REDACTION_TEST"


class FakeHubClient:
    def __init__(self, sink):
        self.sink = sink

    def client_result(self, cid, rid, **kw):
        self.sink.append((rid, kw))


class FakeRunner:
    def __init__(self, stdout="ran", exit_code=0):
        self.calls = []
        self.result = {"exit_code": exit_code, "stdout": stdout,
                       "stderr": "", "timed_out": False, "cancelled": False}

    def __call__(self, argv, cwd, env, timeout, on_process=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env),
                           "timeout": timeout})
        return dict(self.result)


def make_runtime(executors, approve, asked=None):
    """Enough of a ClientRuntime for _dispatch_request (the appedit/typing
    test pattern): records every approval question and every hub result."""
    rt = object.__new__(ct.ClientRuntime)
    rt.executors = dict(executors)
    rt.client_id = "test-client"
    rt.approval_timeout = 10.0
    rt.results = []
    rt.asked = asked if asked is not None else []
    rt.hub = FakeHubClient(rt.results)
    rt.ask_approval = lambda summary, timeout=None: (rt.asked.append(summary)
                                                     or approve)
    return rt


def dispatch(rt, tool, args, needs_approval=False):
    ct.ClientRuntime._dispatch_request(
        rt, {"request_id": f"r-{tool}", "tool": tool, "arguments": args,
             "needs_approval": needs_approval,
             "summary": f"{tool} {args}"}, 10.0)
    return rt.results[-1][1]


@pytest.fixture
def pack(tmp_path):
    """A command pack with fake home + fake runner (no register_extra
    side effects; the executors only)."""
    home = tmp_path / "home"
    home.mkdir()
    runner = FakeRunner()
    service = commands_pack.CommandService(cfg={}, home=str(home),
                                           runner_fn=runner,
                                           env={"PATH": "/bin"})
    return service, runner


# ── auto-run: output comes back without any ask ────────────────
def test_auto_path_no_ask(pack):
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "run_command", {"command": "whoami"})
    assert out["ok"] is True
    assert out["result"]["auto"] is True
    assert out["result"]["stdout"] == "ran"
    assert rt.asked == []
    assert runner.calls[0]["argv"] == ["whoami"]


def test_recipes_tool_dispatch(pack):
    """command_recipes rides the same executors: lookup through the
    runtime, read-only, never asks, never execs."""
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "command_recipes",
                   {"query": "how much space is free on the root volume"})
    assert out["ok"] is True
    hits = out["result"]["recipes"]
    assert hits and hits[0]["command"] == "df -h /"
    assert rt.asked == [] and runner.calls == []


# ── approval flow ──────────────────────────────────────────────
def test_non_allowlisted_asks_once_with_command_and_cwd(pack):
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "run_command",
                   {"command": "touch new", "cwd": str(Path.home())})
    assert out["ok"] is True and out["result"]["auto"] is False
    # exactly ONE question (permission_default always: no hub pre-gate)
    assert len(rt.asked) == 1
    assert "touch new" in rt.asked[0] and str(Path.home()) in rt.asked[0]
    assert runner.calls[0]["argv"] == ["touch", "new"]


def test_deny_does_not_run(pack):
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=False)
    out = dispatch(rt, "run_command", {"command": "touch new"})
    assert out["ok"] is False and out["approved"] is False
    assert "denied" in out["error"]
    assert runner.calls == []


def test_hub_approval_alone_still_asks(pack):
    """needs_approval=True (generic hub gate approved) injects
    confirmed=True — the service must STILL ask, showing the exact
    command, and only its own token may grant."""
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "run_command", {"command": "touch new"},
                   needs_approval=True)
    assert out["ok"] is True
    # ask #1 = the hub gate summary, ask #2 = OUR bubble with the command
    assert len(rt.asked) == 2
    assert rt.asked[1].startswith("run command:") and "touch new" in rt.asked[1]
    assert len(runner.calls) == 1


def test_hub_gate_denied_never_reaches_executor(pack):
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=False)
    out = dispatch(rt, "run_command", {"command": "touch new"},
                   needs_approval=True)
    assert out["ok"] is False and len(rt.asked) == 1
    assert runner.calls == []


def test_secret_cwd_ask_warns(pack, tmp_path):
    service, _ = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    dispatch(rt, "run_command", {"command": "ls",
                                 "cwd": str(tmp_path / "home" / ".ssh")})
    assert "secrets" in rt.asked[0]


def test_refusal_posts_error(pack):
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "run_command", {"command": "sudo reboot"})
    assert out["ok"] is False and "sudo" in out["error"]
    assert runner.calls == []


def test_bad_arguments_post_error(pack):
    service, runner = pack
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "run_command", {})       # no command at all
    assert out["ok"] is False
    assert runner.calls == []


def test_output_redacted_before_the_model_sees_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runner = FakeRunner(stdout=f"token=sk-{SECRET.lower()}")
    service = commands_pack.CommandService(cfg={}, home=str(home),
                                           runner_fn=runner, env={})
    rt = make_runtime(commands_pack.command_executors(service), approve=True)
    out = dispatch(rt, "run_command", {"command": "whoami"})
    assert SECRET not in out["result"]["stdout"]
    assert "[redacted]" in out["result"]["stdout"]


# ── registration: rides the awareness gate via register_extra ──
class FakeAwareness:
    def __init__(self, enabled):
        self.enabled = enabled

    def set(self, v):
        self.enabled = bool(v)
        return self.enabled


class FakeSense:
    def __init__(self, enabled=True):
        self.awareness = FakeAwareness(enabled)


def test_install_registers_and_gates(tmp_path):
    providers_before = list(ct._EXTRA_PROVIDERS)
    log = []
    pack_obj = commands_pack.install(
        sense=FakeSense(), cfg={"commands": {"enabled": True}},
        runner_fn=FakeRunner(), env={}, printer=log.append)
    try:
        assert pack_obj is not None
        assert pack_obj.provider in ct._EXTRA_PROVIDERS
        # awareness on -> the catalogue carries the pack (permission
        # default 'always': the desktop decides per call)
        tools, ex = ct._collect_extra(FakeSense())
        names = [t["name"] for t in tools]
        assert "run_command" in names and "command_recipes" in names
        assert {"run_command", "command_recipes"} <= set(ex)
        spec = next(t for t in tools if t["name"] == "run_command")
        assert spec["permission_default"] == "always"
        # awareness off -> desktop_tools hides the whole pack
        assert "run_command" in [t["name"] for t in ct.desktop_tools(FakeSense())]
        assert "run_command" not in [t["name"] for t in
                                     ct.desktop_tools(FakeSense(enabled=False))]
    finally:
        ct.unregister_extra(pack_obj.provider)
        assert ct._EXTRA_PROVIDERS == providers_before


def test_install_disabled_returns_none():
    assert commands_pack.install(sense=FakeSense(),
                                 cfg={"commands": {"enabled": False}}) is None


def test_install_denied_audit_line(tmp_path):
    """Denials are logged even though the runtime never re-calls the
    executor after 'no' (install wraps ask_approval for exactly this)."""
    log = []
    rt = object.__new__(ct.ClientRuntime)
    rt.ask_approval = lambda summary, timeout=None: False   # owner says no
    pack_obj = commands_pack.install(
        sense=FakeSense(enabled=False), cfg={}, runtime=rt,
        runner_fn=FakeRunner(), env={}, printer=log.append)
    try:
        assert rt.ask_approval("run command: `touch x` in /tmp", 5) is False
        assert any(line.startswith("[cmd] denied") and "touch x" in line
                   for line in log)
        # unrelated asks (other tools' bubbles) are not ours to log
        rt.ask_approval("type 12 chars into kitty", 5)
        assert sum(1 for l in log if l.startswith("[cmd] denied")) == 1
    finally:
        ct.unregister_extra(pack_obj.provider)


# ── hub prompt line (registry) ─────────────────────────────────
def test_prompt_line_only_when_offered():
    off = tools_prompt(["web_search"])
    assert "run_command" not in off
    on = tools_prompt(["desktop__run_command", "web_search"])
    assert "run_command" in on and "sudo" in on.lower()
    assert "command_recipes" in on  # recipe lookups in the same line
    # one line only, whatever the spelling of the name
    assert tools_prompt(["desk__run_command"]).count("run_command and") == 1


# ── config block parses (YAML bools/strings, see merge note) ───
def test_character_config_commands_block():
    path = Path(__file__).resolve().parent.parent / "character_config.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = cfg["commands"]
    assert block["enabled"] is True
    assert block["auto_allow"] == []
    assert block["recipes"] == []           # defaults stand; owner extends
    assert commands_pack.merge_recipes(block) == \
        [dict(r) for r in commands_pack.DEFAULT_RECIPES]
