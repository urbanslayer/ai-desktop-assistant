"""riko.appedit — Obsidian + Kate editing tools and find_file (docs/TYPING.md).

`tools()` returns the client-tool catalogue (same shape as
riko/client_tools.py and riko/desktop_sense/sense.py specs: name,
description, parameters JSON schema, risk, permission_default);
`executors(sense=…)` returns the name -> callable map a ClientRuntime
runs. desktop.py wires both with `install(sense, runtime=…, bridge=…)`,
which registers the pack through client_tools.register_extra so the
tools appear with the awareness toggle and vanish with it.

Obsidian talks to the Local REST API plugin only over loopback; the API
key never leaves ~/.config/riko/obsidian.json. Kate is driven through a
runner the tests fake; edits go to disk with backups and diffs in the
approval. Nothing here starts a compositor or reads the live session
unless a DesktopSense is passed in by the caller.
"""
from . import files, kate, obsidian
from .obsidian import ObsidianRest, load_settings, save_key, key_is_set


def tools() -> list:
    """The full app-editing catalogue: obsidian_*, kate_*, find_file."""
    return [*obsidian.obsidian_tools(), *kate.kate_tools(),
            *kate.kate_find_tools()]


def executors(sense=None, home=None) -> dict:
    """Executors for tools(). `sense` (a DesktopSense or None) is used by
    kate_tabs / the kate_edit unsaved-tab warning only."""
    return {**obsidian.obsidian_executors(),
            **kate.kate_executors(sense=sense, home=home)}


def install(sense, *, runtime=None, bridge=None, home=None):
    """Register the app-editing pack with client_tools (docs/TYPING.md).

    Same hook the typing pack uses (client_tools.register_extra), so
    obsidian_* / kate_* / find_file appear with the awareness gate and
    vanish with it; the hub re-registration in toggle_awareness picks them
    up without a restart. Call AFTER start_desktop — it needs the
    DesktopSense. With a runtime and awareness already on, republish the
    catalogue right away so the hub sees the new tools immediately.
    Returns the provider (pass to client_tools.unregister_extra to undo).
    """
    from .. import client_tools

    def provider(sense=None):
        return tools(), executors(sense=sense, home=home)

    client_tools.register_extra(provider)
    if runtime is not None and sense is not None \
            and getattr(sense, "awareness", None) is not None \
            and sense.awareness.enabled:
        runtime.set_tools(client_tools.desktop_tools(sense),
                          client_tools.make_executors(bridge, sense,
                                                      runtime.awareness_toggle))
    return provider


__all__ = ["tools", "executors", "install", "obsidian", "kate", "files",
           "ObsidianRest", "load_settings", "save_key", "key_is_set"]
