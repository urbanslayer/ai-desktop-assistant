"""character_config.local.yaml override: deep-merge loader, single entry
point, and the promise that nothing writes the config back.

Rules live in docs/CONFIG.md; the loader is riko/config.py:load_config().
"""
import re
from pathlib import Path

import pytest

from riko.config import deep_merge, local_override_path, load_config

REPO = Path(__file__).resolve().parent.parent

BASE_YAML = """\
hub:
  mode: remote
  url: http://192.0.2.51:8002
  api_key_env: RIKO_HUB_KEY
desktop:
  width: 420
  shortcuts:
    talk: "Space"
    wave: "W"
asr.vocabulary: ["Furina", "Riko"]
tts: hub
"""


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


# ── deep_merge semantics ─────────────────────────────────────
def test_deep_merge_nested_dicts_merge_keys():
    base = {"a": {"x": 1, "deep": {"k1": 1, "k2": 2}}, "b": 2}
    over = {"a": {"deep": {"k2": 20, "k3": 30}, "y": 9}, "c": 3}
    out = deep_merge(base, over)
    assert out == {"a": {"x": 1, "y": 9, "deep": {"k1": 1, "k2": 20, "k3": 30}},
                   "b": 2, "c": 3}
    # inputs not mutated
    assert base["a"]["deep"] == {"k1": 1, "k2": 2}


def test_deep_merge_lists_and_scalars_replace():
    assert deep_merge({"v": [1, 2, 3]}, {"v": [9]})["v"] == [9]
    assert deep_merge({"s": "old"}, {"s": "new"})["s"] == "new"
    # a dict replaced by a scalar replaces (and vice versa)
    assert deep_merge({"d": {"k": 1}}, {"d": "off"})["d"] == "off"
    assert deep_merge({"d": "off"}, {"d": {"k": 1}})["d"] == {"k": 1}


# ── load_config with a local sibling ─────────────────────────
def test_local_override_deep_merges_on_top(tmp_path):
    base = _write(tmp_path, "character_config.yaml", BASE_YAML)
    _write(tmp_path, "character_config.local.yaml", """\
hub:
  url: http://192.0.2.51:8002     # the owner's real box
desktop:
  width: 640
  shortcuts:
    wave: "V"                       # changed
    listen_mode: "L"                # added
asr.vocabulary: ["Only", "These"]   # list REPLACES, not appends
""")
    cfg = load_config(base)
    assert cfg["hub"]["url"] == "http://192.0.2.51:8002"
    assert cfg["hub"]["mode"] == "remote"        # untouched base keys survive
    assert cfg["hub"]["api_key_env"] == "RIKO_HUB_KEY"
    assert cfg["desktop"]["width"] == 640
    assert cfg["desktop"]["shortcuts"] == {"talk": "Space", "wave": "V",
                                           "listen_mode": "L"}
    assert cfg["asr.vocabulary"] == ["Only", "These"]
    assert cfg["tts"] == "hub"


def test_missing_local_file_is_not_an_error(tmp_path):
    base = _write(tmp_path, "character_config.yaml", BASE_YAML)
    cfg = load_config(base)
    assert cfg["hub"]["url"] == "http://192.0.2.51:8002"


def test_empty_local_file_is_a_noop(tmp_path):
    base = _write(tmp_path, "character_config.yaml", BASE_YAML)
    _write(tmp_path, "character_config.local.yaml", "")
    assert load_config(base)["hub"]["url"] == "http://192.0.2.51:8002"


def test_malformed_local_file_error_names_the_file(tmp_path):
    base = _write(tmp_path, "character_config.yaml", BASE_YAML)
    bad = _write(tmp_path, "character_config.local.yaml",
                 "hub:\n  url: [unclosed\n")
    with pytest.raises(SystemExit) as ei:
        load_config(base)
    msg = str(ei.value)
    assert "character_config.local.yaml" in msg   # names the BAD file
    assert "not valid YAML" in msg
    assert "character_config.yaml:" not in msg    # not blamed on the base


def test_non_mapping_local_file_names_the_file(tmp_path):
    base = _write(tmp_path, "character_config.yaml", BASE_YAML)
    _write(tmp_path, "character_config.local.yaml", "- just\n- a\n- list\n")
    with pytest.raises(SystemExit) as ei:
        load_config(base)
    assert "character_config.local.yaml" in str(ei.value)
    assert "mapping" in str(ei.value)


def test_malformed_base_file_also_names_itself(tmp_path):
    bad = _write(tmp_path, "character_config.yaml", "\t oops: [broken\n")
    with pytest.raises(SystemExit) as ei:
        load_config(bad)
    assert "character_config.yaml" in str(ei.value)


def test_env_override_gets_its_own_sibling(tmp_path, monkeypatch):
    """$RIKO_CONFIG elsewhere -> the .local.yaml is looked up NEXT TO IT
    (and the repo's own sibling must not leak in)."""
    nest = tmp_path / "nested"
    nest.mkdir()
    alt = _write(nest, "alt_config.yaml", "hub:\n  url: http://base\n")
    _write(nest, "alt_config.local.yaml", "hub:\n  url: http://local\n")
    # a decoy sibling next to the REPO config must be ignored
    decoy = tmp_path / "repo"
    decoy.mkdir()
    _write(decoy, "character_config.yaml", "marker: repo\n")
    _write(decoy, "character_config.local.yaml", "marker: repo-local\n")
    monkeypatch.setenv("RIKO_CONFIG", str(alt))
    assert load_config()["hub"]["url"] == "http://local"
    monkeypatch.setenv("RIKO_CONFIG", str(decoy / "character_config.yaml"))
    assert load_config()["marker"] == "repo-local"
    monkeypatch.delenv("RIKO_CONFIG")
    assert load_config(nest / "alt_config.yaml")["hub"]["url"] == "http://local"


def test_local_override_path_rule():
    assert local_override_path(Path("/x/character_config.yaml")).name \
        == "character_config.local.yaml"
    assert local_override_path(Path("/x/nested/alt_config.yml")).as_posix() \
        == "/x/nested/alt_config.local.yaml"


# ── the shipped base file carries no real LAN address ────────
def test_shipped_config_uses_documentation_range():
    text = (REPO / "character_config.yaml").read_text(encoding="utf-8")
    assert "192.168.1." not in text
    assert "192.0.2.51" in text


def test_gitignore_covers_local_overrides():
    rules = set((REPO / ".gitignore").read_text().split())
    assert "character_config.local.yaml" in rules
    assert "*.local.yaml" in rules
    # the example template stays tracked
    assert (REPO / "character_config.local.example.yaml").exists()


# ── single entry point + no writers (grep-style guards) ──────
RUNTIME_TARGETS = ["desktop.py", "main.py", "riko", "hub", "server", "bin"]
CONFIG_RE = re.compile(r"character_config(?:\.\w+)*\.yaml")


def _runtime_py_files():
    skip = {".venv", "node_modules", "__pycache__", ".git", "dist"}
    for t in RUNTIME_TARGETS:
        p = REPO / t
        files = [p] if p.suffix == ".py" else sorted(p.rglob("*.py"))
        for f in files:
            if skip & set(f.relative_to(REPO).parts):
                continue
            yield f


def test_only_the_shared_loader_reads_the_config_yaml():
    """No module beside riko/config.py parses YAML or pokes $RIKO_CONFIG —
    i.e. every loader goes through riko.config.load_config()."""
    offenders = []
    for f in _runtime_py_files():
        if f.name == "config.py" and f.parent.name == "riko":
            continue
        text = f.read_text(encoding="utf-8")
        if "yaml.safe_load" in text or "yaml.load(" in text \
                or 'environ.get("RIKO_CONFIG")' in text \
                or "environ.get('RIKO_CONFIG')" in text:
            offenders.append(str(f.relative_to(REPO)))
    assert not offenders


@pytest.mark.parametrize("mod", [
    "desktop.py", "main.py", "riko/asr.py", "riko/tts.py",
    "riko/hub_client.py", "riko/migrate.py",
])
def test_consumers_route_through_load_config(mod):
    text = (REPO / mod).read_text(encoding="utf-8")
    assert "load_config" in text
    assert re.search(r"from riko\.config import[^\n]*load_config", text) \
        or re.search(r"import riko\.config", text)


def test_no_runtime_code_writes_the_config_back():
    """The app must never write character_config*.yaml: no yaml dumps at
    all in runtime code, and no open(...config..., 'w')."""
    problems = []
    for f in _runtime_py_files():
        text = f.read_text(encoding="utf-8")
        if "yaml.dump" in text or "yaml.safe_dump" in text:
            problems.append(f"{f.relative_to(REPO)}: yaml dump")
        for m in re.finditer(r"open\(([^)]*)\)", text):
            args = m.group(1)
            if "character_config" in args and re.search(r"['\"][wax]", args):
                problems.append(f"{f.relative_to(REPO)}: write-mode open")
    assert not problems


def test_loading_with_local_override_never_touches_base_file(tmp_path):
    """Behavioral guard: load_config is read-only — base bytes unchanged,
    and a load can't have 'copied' local values into the base."""
    base = _write(tmp_path, "character_config.yaml", BASE_YAML)
    _write(tmp_path, "character_config.local.yaml",
           "hub:\n  url: http://real\n")
    before = base.read_bytes()
    cfg = load_config(base)
    assert cfg["hub"]["url"] == "http://real"
    assert base.read_bytes() == before
    # and runtime writers (appstate) land in the appstate dir, not here
    from riko import appstate
    monkey_state = tmp_path / "state"
    import os
    old = os.environ.get("RIKO_APP_STATE_DIR")
    os.environ["RIKO_APP_STATE_DIR"] = str(monkey_state)
    try:
        appstate.save_mode("always")
    finally:
        if old is None:
            os.environ.pop("RIKO_APP_STATE_DIR", None)
        else:
            os.environ["RIKO_APP_STATE_DIR"] = old
    assert base.read_bytes() == before
    assert list(tmp_path.glob("*.local.yaml")) == [tmp_path / "character_config.local.yaml"]


def test_backup_pull_script_has_no_personal_host():
    text = (REPO / "scripts" / "backup_pull.sh").read_text()
    assert "192.168.1." not in text
    assert "RIKO_BACKUP_REMOTE" in text
