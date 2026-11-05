"""Docker packaging regression tests for the hub image.

Round 1 crash-looped the deployed container: the Dockerfile listed the files
to COPY by hand and missed the new modules (clients.py, emotion.py,
events.py, toolloop.py, tools/), and compose.yaml never forwarded
RIKO_SEARXNG_URL, so web_search had no backend inside Docker. These tests
parse the real hub/Dockerfile, hub/.dockerignore, hub/compose.yaml and the
hub source so the same miss fails CI instead of production.

A later round put `TZ:` directly under `services.hub` (outside
`environment:`); real `docker compose` refuses the file
("services.hub.TZ false schema"), so the compose files are now also checked
against the Compose spec's key sets, and against the docker CLI when present.
"""
import ast
import fnmatch
import os
import re
import shutil
import subprocess
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def test_compose_sets_the_owner_timezone_for_timers():
    """docs/TIMERS.md H: timers and spoken times follow the owner's wall
    clock — compose pins the container TZ (and the poll knob is forwarded)."""
    svc = COMPOSE["services"]["hub"]
    # TZ must live under environment: (a service-level `TZ:` key made real
    # `docker compose` reject the file: "services.hub.TZ false schema").
    assert "TZ" not in svc, "TZ is a top-level service key; compose rejects it"
    assert svc["environment"]["TZ"] == "${TZ:-Europe/London}"
    assert svc["environment"]["RIKO_TIMER_POLL_S"] == "${RIKO_TIMER_POLL_S:-1}"
    env_example = (HUB / ".env.example").read_text(encoding="utf-8")
    assert "TZ=Europe/London" in env_example
    assert "RIKO_TIMER_POLL_S" in env_example
    # zoneinfo needs tzdata inside python:3.12-slim for Europe/London to resolve
    dockerfile = (HUB / "Dockerfile").read_text(encoding="utf-8")
    assert "tzdata" in dockerfile


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

# --------------------------------------------------------------------------
# Compose file shape (hub/ + tts/ + server/): the TZ-as-service-key bug made
# `docker compose` reject hub/compose.yaml outright; catch that class of bug
# statically, and against the docker CLI when it is available.
# --------------------------------------------------------------------------

# compose-spec service-level attributes (compose-spec/compose-spec), plus
# a few still-accepted legacy keys; extension keys (x-*) are always allowed.
ALLOWED_COMPOSE_SERVICE_KEYS = {
    "annotations", "attach", "blkio_config", "build", "cap_add", "cap_drop",
    "cgroup", "cgroup_parent", "container_name", "cpu_count", "cpu_percent",
    "cpu_period", "cpu_quota", "cpu_rt_period", "cpu_rt_runtime",
    "cpu_shares", "cpus", "cpuset", "credential_spec", "depends_on",
    "deploy", "develop", "device_cgroup_rules", "devices", "dns", "dns_opt",
    "dns_search", "domainname", "entrypoint", "env_file", "environment",
    "expose", "extends", "extensions", "gpus", "group_add", "healthcheck",
    "hostname", "image", "init", "ipc", "isolation", "labels", "links",
    "logging", "mac_address", "mem_limit", "mem_reservation",
    "mem_swappiness", "memswap_limit", "memory", "models", "mounts",
    "network_mode", "networks", "oom_kill_disable", "oom_score_adj", "pid",
    "pids_limit", "platform", "ports", "privileged", "profiles", "provider",
    "pull_policy", "read_only", "restart", "runtime", "scale", "secrets",
    "security_opt", "shm_size", "sidecars", "stdin_open", "stop_grace_period",
    "stop_signal", "storage_opt", "sysctls", "tmpfs", "tty", "ulimits",
    "user", "userns_mode", "uts", "volumes", "volumes_from", "working_dir",
}

ALLOWED_COMPOSE_TOP_KEYS = {
    "name", "services", "networks", "volumes", "configs", "secrets", "include",
}

COMPOSE_DIRS = (ROOT / "hub", ROOT / "tts", ROOT / "server")


def _load_compose(path):
    """Parse a compose file with PyYAML, tolerating Compose-only YAML tags
    (!reset, !override, ...) that the tts/server override files use."""
    class _Loader(yaml.SafeLoader):
        pass

    def _construct_any(loader, _suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _Loader.add_multi_constructor("!", _construct_any)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def _compose_docker_runs():
    """(files, env) pairs for `docker compose config -q`. Base files validate
    alone; compose.<flavour>.yaml files are override layers (server/compose.
    cpu.yaml has no image/build at all) and must ride on top of the base.
    VIDEO_GID/RENDER_GID get placeholders because tts/compose.amd.yaml
    interpolates them from the host without defaults."""
    runs = []
    for d in COMPOSE_DIRS:
        files = sorted(d.glob("compose*.y*ml"))
        base = next((f for f in files
                     if f.name in ("compose.yaml", "compose.yml")), None)
        for f in files:
            stack = [f] if f == base or base is None else [base, f]
            runs.append((stack, {"VIDEO_GID": "444", "RENDER_GID": "999"}))
    return runs


def test_compose_files_use_only_valid_compose_keys():
    """Every top-level and per-service key must be a real Compose spec key.
    This is the shape check that `docker compose` performs; a stray `TZ:`
    (or any other env var) at the wrong level fails here, not on the box."""
    files = [f for d in COMPOSE_DIRS for f in sorted(d.glob("compose*.y*ml"))]
    assert files, "no compose files found"
    for path in files:
        doc = _load_compose(path)
        rel = path.relative_to(ROOT)
        assert isinstance(doc, dict), f"{rel}: empty or non-mapping file"
        top_bad = {k for k in doc
                   if k not in ALLOWED_COMPOSE_TOP_KEYS and not str(k).startswith("x-")}
        assert not top_bad, f"{rel}: unknown top-level key(s): {sorted(top_bad)}"
        for svc_name, svc in (doc.get("services") or {}).items():
            assert isinstance(svc, dict), f"{rel}: service {svc_name} is not a mapping"
            bad = {k for k in svc
                   if k not in ALLOWED_COMPOSE_SERVICE_KEYS
                   and not str(k).startswith("x-")}
            assert not bad, (
                f"{rel}: service {svc_name} has key(s) that are not Compose "
                f"service keys: {sorted(bad)} (env vars belong under "
                "`environment:` — a top-level TZ broke deployment this way)")


def test_docker_compose_cli_accepts_every_compose_file():
    """Ground truth: run `docker compose config -q` over every stack the way
    the README deploys it (override flavours layered on the base). Skipped
    when docker is missing or its daemon is unreachable."""
    docker = shutil.which("docker")
    if docker is None:
        import pytest
        pytest.skip("docker CLI not on PATH")
    env = {**os.environ}
    for path, extra_env in _compose_docker_runs():
        env.update(extra_env)
        args = [docker, "compose"]
        for f in path:
            args += ["-f", str(f)]
        proc = subprocess.run(args + ["config", "-q"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 and ("Cannot connect to the Docker daemon" in proc.stderr
                                     or "failed to connect to the docker API" in proc.stderr):
            import pytest
            pytest.skip("docker daemon unreachable")
        shown = " + ".join(str(f.relative_to(ROOT)) for f in path)
        assert proc.returncode == 0, (
            f"docker compose config failed for {shown}:\n{proc.stderr.strip()}")

