"""riko.commands — run_command: Riko runs commands on the owner's PC and
reads their output back (docs/COMMANDS.md).

Desktop client tool registered through client_tools.register_extra, so it
appears with the desktop-awareness gate and vanishes with it. Hub
permission_default is `always`: the DESKTOP decides per call — allowlisted
read-only commands run at once, everything else asks through the
client-side approval bubble showing the exact command and working
directory (a generic hub approval never substitutes for that ask; see
service.CommandService's one-time cmd_token). Nothing is ever elevated.

install() is the entry point desktop.py calls; the runner is injectable
and tests/conftest.py hard-fails the real subprocess path for this
package, so tests never execute real commands.
"""
from __future__ import annotations

from .policy import (DEFAULT_TIMEOUT, MAX_TIMEOUT, Decision, decide,
                     has_shell_syntax, touches_secret)
from .recipes import (DEFAULT_RECIPES, compact_desc, lookup, merge_recipes,
                      validate as validate_recipes)
from .service import ASK_PREFIX, CMD_TOKEN_TTL, CommandService


def command_tools(recipes=None) -> list:
    """The client-tool catalogue entries (same shape as the other packs)."""
    obj = {"type": "object", "additionalProperties": False}
    desc_block = compact_desc(recipes if recipes is not None
                              else DEFAULT_RECIPES)
    return [
        {"name": "run_command", "risk": "act", "permission_default": "always",
         "description": (
             "Run one terminal command on the owner's PC and read its "
             "output: {exit_code, stdout, stderr, truncated}. Allowlisted "
             "read-only commands (ls, cat, df, git status, ps, …) run "
             "right away; anything else — and anything with shell syntax "
             "(| ; && > $() globs) — asks the owner first, who sees the "
             "exact command and working directory. NEVER elevated (sudo/su/"
             "pkexec refused outright), no interactive programs (use cat, "
             "top -b -n1), output capped at 8000 chars per stream. You "
             "cannot widen the allowlist yourself. Returns quickly: "
             "timeout default 20 s, max 120 s. Common asks -> command: "
             + desc_block + " (more via command_recipes)."),
         "parameters": dict(obj, properties={
             "command": {"type": "string",
                         "description": "the exact command line to run"},
             "cwd": {"type": "string",
                     "description": f"working directory (default $HOME; "
                                    f"outside $HOME//tmp needs approval)"},
             "timeout": {"type": "integer",
                         "description": f"seconds, default "
                                        f"{int(DEFAULT_TIMEOUT)}, max "
                                        f"{int(MAX_TIMEOUT)}"}},
             required=["command"])},
        {"name": "command_recipes", "risk": "read",
         "permission_default": "always",
         "description": (
             "Look up the right command for a question about this PC "
             "(disk space, memory, CPU, GPU, temperatures, processes, "
             "services, logs, network, packages, updates, git, docker). "
             "Pass a few words of the owner's own phrasing as query; "
             "returns ranked {ask, command, note?, asks?} recipes — then "
             "run the command with run_command (asks: true means the "
             "owner's approval bubble will pop). No query = full list."),
         "parameters": dict(obj, properties={
             "query": {"type": "string",
                       "description": "the owner's question, or key words"}})},
    ]


def command_executors(service: CommandService, recipes=None) -> dict:
    cat = service.recipes if recipes is None else recipes

    def command_recipes_tool(query=None, **_kw):
        hits = lookup(str(query or ""), cat)
        out = {"count": len(hits), "recipes": hits}
        if not hits:
            out["note"] = ("no recipe matched — pick a plain read-only "
                           "command yourself (df, free, ps, journalctl -n) "
                           "or call command_recipes with no query")
        else:
            out["note"] = ("run the command with run_command; recipes "
                           "marked asks:true will ask the owner first")
        return out

    return {"run_command": service.run,
            "command_recipes": command_recipes_tool}


class CommandPack:
    """What desktop.py keeps: the service (for cancel) + the provider."""

    def __init__(self, service, provider):
        self.service = service
        self.provider = provider

    @property
    def recipes(self):
        return self.service.recipes

    def cancel(self):
        return self.service.cancel()


def install(sense=None, *, cfg=None, runtime=None, bridge=None,
            runner_fn=None, env=None, printer=print):
    """Register the command pack with client_tools (docs/COMMANDS.md).

    Same hook the typing/app-editing packs use, so run_command rides the
    awareness gate; with a runtime and awareness already on, republish the
    catalogue right away so the hub sees it without a restart. `cfg` is
    the whole config (the `commands:` block selects enabled/auto_allow).
    Returns the CommandPack, or None when `commands.enabled: false`.
    """
    from .. import client_tools

    block = (cfg or {}).get("commands") if isinstance(cfg, dict) else None
    if not (block or {}).get("enabled", True):
        return None
    recipes = merge_recipes(block)
    for problem in validate_recipes(recipes):
        # catalogue honesty: log, never crash — a custom entry that cannot
        # auto-run will simply ask (or refuse) at run time via policy.
        printer(f"[cmd] recipe warning: {problem}")
    service = CommandService(cfg=block, runner_fn=runner_fn, env=env,
                             printer=printer, recipes=recipes)

    def provider(sense=None):
        return command_tools(recipes), command_executors(service, recipes)

    client_tools.register_extra(provider)

    # Audit 'denied' (docs/COMMANDS.md rule 9): the runtime never re-calls
    # the executor after an owner denial, so watch the answer here. Only
    # asks whose summary we minted (ASK_PREFIX) are logged.
    ask = getattr(runtime, "ask_approval", None) if runtime is not None else None
    if runtime is not None and ask is not None \
            and not getattr(runtime, "_riko_cmd_audit", False):
        def _ask_with_audit(summary, timeout=None, _orig=ask):
            ok = _orig(summary, timeout)
            s = str(summary or "")
            if not ok and s.startswith(ASK_PREFIX):
                printer(f"[cmd] denied {s[len(ASK_PREFIX):].strip()} "
                        f"exit=- -")
            return ok
        runtime.ask_approval = _ask_with_audit
        runtime._riko_cmd_audit = True

    if runtime is not None and sense is not None \
            and getattr(sense, "awareness", None) is not None \
            and sense.awareness.enabled:
        runtime.set_tools(client_tools.desktop_tools(sense),
                          client_tools.make_executors(bridge, sense,
                                                      runtime.awareness_toggle))
    return CommandPack(service, provider)


__all__ = ["ASK_PREFIX", "CMD_TOKEN_TTL", "CommandPack", "CommandService",
           "Decision", "decide", "has_shell_syntax", "touches_secret",
           "command_tools", "command_executors", "install",
           "DEFAULT_RECIPES", "compact_desc", "lookup", "merge_recipes",
           "validate_recipes", "DEFAULT_TIMEOUT", "MAX_TIMEOUT"]
