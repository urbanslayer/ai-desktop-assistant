"""The avatar page must run fully offline (BACKLOG quick win 6).

Everything the page loads at runtime is served from client/vendor/ (pinned
versions, licence texts and hashes recorded in client/vendor/README.md).
These tests assert:
  1. no http(s) URL is referenced by client/index.html or client/js/*.js
     (nothing to fetch at runtime),
  2. the import map points into ./vendor/ and every import used by the page
     resolves to a file that exists,
  3. the dependency closure of the vendored modules is local: relative
     imports exist on disk, bare specifiers are in the import map,
  4. each vendored file still matches the SHA-256 recorded in the README,
     and each vendored lib ships its licence file next to it.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "client"
VENDOR = CLIENT / "vendor"
PAGE = CLIENT / "index.html"

SPEC_RE = re.compile(r"""(?:\bfrom|import)\s*\(?\s*['"]([^'"\s]+)['"]""")
KEYWORD_RE = re.compile(r"\b(?:import|export)\b")
ROW_RE = re.compile(r"^\|\s*[^|]+\|\s*[^|]+\|\s*`([^`]+)`\s*\|\s*[^|]+\|\s*`([0-9a-f]{64})`\s*\|")


def _imports(src: str) -> list:
    """Import specifiers in JS source. Comments and template literals are stripped
    (template literals hold prose like 'colors from "srgb-linear"' that must not
    parse as imports); multi-line `import {\\n ... } from 'x';` statements are kept
    by joining a brace run into one line before matching."""
    src = _strip_comments(src)
    src = re.sub(r"`(?:[^`\\]|\\.)*`", "''", src, flags=re.S)   # template literals -> ''
    lines = []
    buf = ""
    depth = 0
    for line in src.splitlines():
        buf += line + " "
        depth += line.count("{") - line.count("}")
        if depth <= 0 or "from" in buf or "import(" in buf:
            lines.append(buf)
            buf, depth = "", max(depth, 0)
    lines.append(buf)
    specs = []
    for line in lines:
        if KEYWORD_RE.search(line):
            specs.extend(SPEC_RE.findall(line))
    return specs


def _import_map() -> dict:
    html = PAGE.read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    assert m, "index.html lost its import map"
    return json.loads(m.group(1))["imports"]


def _module_sources() -> dict:
    """index.html's module script(s) plus every client/js/*.js file."""
    srcs = {PAGE: PAGE.read_text(encoding="utf-8")}
    for js in sorted((CLIENT / "js").glob("*.js")):
        srcs[js] = js.read_text(encoding="utf-8")
    return srcs


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)   # block comments
    src = re.sub(r"(?<!:)//[^\n]*", "", src)          # line comments (keep // in URLs)
    return src


def _resolve(spec: str, importer: Path, imports: dict):
    """Resolve an import specifier the way the browser would (import map first).
    Returns a Path, or None for bare specifiers with no map entry (node builtins
    in *.test.js), or the string 'remote' for absolute http(s) URLs."""
    if spec.startswith(("http://", "https://")):
        return "remote"
    if spec in imports:
        return (CLIENT / imports[spec].removeprefix("./")).resolve()
    for prefix, target in imports.items():
        if prefix.endswith("/") and spec.startswith(prefix):
            return (CLIENT / target.removeprefix("./") / spec[len(prefix):]).resolve()
    if spec.startswith("."):
        return (importer.parent / spec).resolve()
    return None


def test_no_remote_urls_in_page_and_js():
    offenders = []
    for path, src in _module_sources().items():
        for m in re.finditer(r"https?://\S+", src):
            offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert not offenders, "runtime files reference remote URLs:\n" + "\n".join(offenders)


def test_import_map_is_local_vendor():
    imports = _import_map()
    assert imports, "empty import map"
    for spec, target in imports.items():
        assert target.startswith("./vendor/"), f"{spec} -> {target} is not vendored"


def test_page_imports_resolve_to_existing_files():
    imports = _import_map()
    m = re.search(r'<script type="module">(.*?)</script>', PAGE.read_text(encoding="utf-8"), re.S)
    assert m
    for spec in _imports(m.group(1)):
        target = _resolve(spec, PAGE, imports)
        assert target is not None, f"page import {spec!r} is not in the import map"
        assert target.is_file(), f"page import {spec!r} -> {target} missing"


def test_vendored_dependency_closure_is_local():
    imports = _import_map()
    queue = sorted(VENDOR.rglob("*.js"))
    seen = set()
    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        for spec in _imports(f.read_text(encoding="utf-8")):
            if re.match(r"^node:", spec):
                continue
            target = _resolve(spec, f, imports)
            assert target != "remote", f"{f.relative_to(VENDOR)}: remote import {spec!r}"
            assert target is not None, f"{f.relative_to(VENDOR)}: bare import {spec!r} not in import map"
            if target.is_file():
                queue.append(target)
            else:
                pytest.fail(f"{f.relative_to(VENDOR)}: import {spec!r} -> {target} missing")


def _readme_manifest() -> list:
    rows = []
    for line in (VENDOR / "README.md").read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def test_vendored_files_match_recorded_sha256():
    manifest = _readme_manifest()
    assert len(manifest) == len(list(VENDOR.rglob("*.js"))), (
        "vendor/README.md file list and client/vendor/*.js are out of sync")
    for rel, digest in manifest:
        f = VENDOR / rel
        assert f.is_file(), f"vendored file missing: {rel}"
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        assert actual == digest, f"{rel}: sha256 {actual} != recorded {digest}"


@pytest.mark.parametrize("lib", ["three", "three-vrm", "three-vrm-animation"])
def test_licence_next_to_each_vendored_lib(lib):
    lic = VENDOR / lib / "LICENSE"
    assert lic.is_file(), f"{lib} has no LICENSE file"
    text = lic.read_text(encoding="utf-8")
    assert "MIT" in text or "Permission is hereby granted, free of charge" in text
