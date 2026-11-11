"""Loads character_config.yaml and the API keys from .env.

load_config() is the ONE place the app reads its config file. If a sibling
``character_config.local.yaml`` sits next to the file being loaded, it is
deep-merged on top: dicts merge recursively, lists and scalars replace.
The local file is the owner's private overrides (real hub / LLM / ASR / TTS
addresses) and is gitignored, so the public repo ships documentation-range
example addresses while the owner's machine keeps its real ones.
See docs/CONFIG.md. The app never writes character_config.yaml — runtime
choices (listen mode, awareness, scale…) live in the appstate files
(riko/appstate.py), so a local override can never leak back into the base.
"""
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # the project folder
load_dotenv(ROOT / ".env")


def deep_merge(base, override):
    """`override` on top of `base`: dicts merge recursively, anything else
    (lists, scalars, None) replaces wholesale. Returns new dicts; never
    mutates its inputs."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml_mapping(path, kind):
    """Read one YAML file that must parse to a mapping (empty file = {})."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"[config] cannot read {kind} file {path}: {exc}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        sys.exit(f"[config] {kind} file {path} is not valid YAML:\n{exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        sys.exit(f"[config] {kind} file {path} must be a YAML mapping, "
                 f"got {type(data).__name__}")
    return data


def local_override_path(path):
    """The private sibling for a config file: character_config.yaml ->
    character_config.local.yaml (the rule follows RIKO_CONFIG targets too)."""
    path = Path(path)
    return path.with_name(f"{path.stem}.local.yaml")


def load_config(path=None):
    """Parsed config: base YAML + optional `<name>.local.yaml` deep-merged.

    Search order: explicit `path` arg > $RIKO_CONFIG > character_config.yaml
    next to this repo. The local override is looked up next to whichever file
    was chosen, so smoke tests pointing RIKO_CONFIG at a temp yaml still get
    (and can still use) their own sibling .local.yaml.
    """
    path = Path(path or os.environ.get("RIKO_CONFIG") or ROOT / "character_config.yaml")
    cfg = _read_yaml_mapping(path, "config")
    local = local_override_path(path)
    if local.exists():
        cfg = deep_merge(cfg, _read_yaml_mapping(local, "local override"))
    return cfg


def need_key(name, why):
    """Stop at startup with a clear message if an API key is missing."""
    if not os.getenv(name):
        sys.exit(f"[setup] {name} is missing. Add it to .env (needed for {why}).")
    print(f"[setup] {name} found ({why})")
