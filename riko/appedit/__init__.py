"""riko.appedit — Obsidian + Kate editing tools and find_file (docs/TYPING.md).

`tools()` returns the client-tool catalogue (same shape as
riko/client_tools.py and riko/desktop_sense/sense.py specs: name,
description, parameters JSON schema, risk, permission_default);
`executors(sense=…)` returns the name -> callable map a ClientRuntime
runs. The one-line registration belongs to the reviewer /
`register_extra(tools)` hook in riko/client_tools.py, e.g.:

    from riko import appedit
    tools += appedit.tools()
    executors.update(appedit.executors(sense=sense))

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


__all__ = ["tools", "executors", "obsidian", "kate", "files",
           "ObsidianRest", "load_settings", "save_key", "key_is_set"]
