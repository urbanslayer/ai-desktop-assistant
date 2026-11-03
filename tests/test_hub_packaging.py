"""Docker packaging regression tests for the hub image.

Round 1 crash-looped the deployed container: the Dockerfile listed the files
to COPY by hand and missed the new modules (clients.py, emotion.py,
events.py, toolloop.py, tools/), and compose.yaml never forwarded
RIKO_SEARXNG_URL, so web_search had no backend inside Docker. These tests
parse the real hub/Dockerfile, hub/.dockerignore, hub/compose.yaml and the
hub source so the same miss fails CI instead of production.
"""
import ast
import fnmatch
import re
from pathlib import Path

import yaml

HUB = Path(__file__).resolve().parents[1] / "hub"
DOCKERFILE = (HUB / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (HUB / ".dockerignore").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((HUB / "compose.yaml").read_text(encoding="utf-8"))

# every hub-owned .py the image must ship (top level + the tools/ package)
HUB_SOURCES = sorted(list(HUB.glob("*.py")) + list((HUB / "tools").glob("*.py")))


def _env_names_read_by_hub_code():
    """Every RIKO_* name the hub reads via os.getenv / os.environ."""
    names = set()
    for path in HUB_SOURCES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "os.getenv" in line or "os.environ" in line:
                names.update(re.findall(r"\bRIKO_[A-Z0-9_]+\b", line))
    return names


def _hub_module_names():
    mods = {p.stem for p in HUB.glob("*.py")}
    mods |= {d.name for d in HUB.iterdir()
             if d.is_dir() and (d / "__init__.py").exists()}
    return mods


def _imports_of(path):
    """Top-level module names `path` imports (absolute or hub-relative)."""
    known = _hub_module_names()
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in known:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # `from . import x` / `from .tools import y`
                if node.module:
                    found.add(node.module.split(".")[0])
                else:                           # `from . import brain` → names hold the modules
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in known:
                            found.add(root)
            elif node.module:
                root = node.module.split(".")[0]
                if root in known:
                    found.add(root)
    return found


def _modules_server_needs():
    """server.py's transitive closure over hub-owned modules."""
    needed = {"server"}
    seen = set()
    while needed - seen:
        mod = (needed - seen).pop()
        seen.add(mod)
        path = HUB / f"{mod}.py" if mod != "tools" else HUB / "tools" / "__init__.py"
        files = [path] + (sorted((HUB / mod).glob("*.py")) if path.is_dir() else [])
        for f in files:
            if f.exists():
                needed |= _imports_of(f)
    return needed


def _copy_rules():
    """(source_list, dest) for every COPY line, ignoring --from= stages."""
    rules = []
    for line in DOCKERFILE.splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY") or "--from=" in line:
            continue
        parts = line.split()[1:]
        if len(parts) >= 2:
            rules.append((parts[:-1], parts[-1]))
    return rules


def _dockerignore_patterns():
    pats = []
    for line in DOCKERIGNORE.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line)
    return pats


def _excluded_by_dockerignore(mod):
    """True if a .dockerignore pattern would drop `mod` (file or dir name)."""
    for pat in _dockerignore_patterns():
        neg = pat.startswith("!")
        pat = pat.lstrip("!").rstrip("/")
        hit = (fnmatch.fnmatch(mod, pat) or mod == pat
               or mod.startswith(pat + "/") or pat.startswith(mod + "/")
               or fnmatch.fnmatch(mod + "/", pat + "/*"))
        if hit:
            return not neg
    return False


def test_compose_forwards_every_env_the_hub_reads():
    missing = _env_names_read_by_hub_code() - set(
        COMPOSE["services"]["hub"]["environment"])
    assert not missing, (
        f"hub code reads {sorted(missing)} but compose.yaml environment "
        "doesn't forward them (web_search lost its backend this way in "
        "round 1)")


def test_compose_env_interpolation_matches_key():
    for key, value in COMPOSE["services"]["hub"]["environment"].items():
        if "${" in str(value):
            assert f"${{{key}" in str(value) or key == "RIKO_HUB_PORT", \
                f"{key}: interpolates a different var ({value!r})"


def test_dockerfile_ships_every_module_server_imports():
    needed = _modules_server_needs()
    assert "clients" in needed and "tools" in needed  # the round-1 casualties
    copy_all = any(srcs == ["."] for srcs, _ in _copy_rules())
    if not copy_all:  # explicit-list Dockerfile: every module must be named
        listed = " ".join(s for srcs, _ in _copy_rules() for s in srcs)
        missing = [m for m in sorted(needed)
                   if m not in listed and f"{m}.py" not in listed]
        assert not missing, f"Dockerfile COPY list misses hub modules: {missing}"


def test_dockerfile_ships_static_and_assets():
    copy_all = any(srcs == ["."] for srcs, _ in _copy_rules())
    listed = " ".join(s for srcs, _ in _copy_rules() for s in srcs)
    for folder in ("static", "assets"):
        assert copy_all or folder in listed, f"{folder}/ never reaches the image"


def test_dockerignore_does_not_exclude_hub_code():
    for mod in sorted(_modules_server_needs()):
        assert not _excluded_by_dockerignore(mod), \
            f".dockerignore excludes {mod}, which server.py imports"
    # the junk stays out
    for junk in (".venv", "data", ".env"):
        assert _excluded_by_dockerignore(junk), f"{junk} would bloat the image"


def test_dependency_layer_is_cached_before_the_source_copy():
    lines = [l.strip() for l in DOCKERFILE.splitlines()]
    deps = next((i for i, l in enumerate(lines)
                 if l.upper().startswith("COPY") and "pyproject.toml" in l), None)
    assert deps is not None, "Dockerfile must COPY pyproject.toml (deps layer)"
    copy_all = next((i for i, l in enumerate(lines)
                     if re.match(r"COPY\s+\.(\s|$)", l, re.IGNORECASE)), None)
    if copy_all is not None:
        assert deps < copy_all, "uv sync deps layer must precede the source COPY"
