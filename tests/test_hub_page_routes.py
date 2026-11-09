"""Regression test: every fetch/api()/audioBlobUrl()/playInto() call in the
web config page must hit a route that hub/server.py actually registers with
the same HTTP method.

The original bug this catches: the Preview button fetched
`/api/personas/{id}/voice/preview` with GET while the route is POST — the
page said "preview failed: Method not allowed" (405) while every curl of
the route worked fine.

The extraction is deliberately dumb (regex over the page script): it only
resolves calls whose path is a string or template literal (`${...}` parts
become one path param each; `"/api/x/" + pid` counts as one param segment;
query strings are dropped). Calls with a runtime variable path
(e.g. playInto(ctx, r.audio_url)) cannot be checked statically and are
listed, not asserted on.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient  # noqa: F401  (keeps fastapi dep obvious)
from fastapi.routing import APIRoute

from hub.server import create_app

PAGE = Path(__file__).resolve().parent.parent / "hub" / "static" / "index.html"

# function -> index of the path argument and of the options argument
CALL_ARGS = {"api": (0, 1), "fetch": (0, 1), "audioBlobUrl": (0, 1), "playInto": (1, 2)}
CALL_RE = re.compile(r"\b(" + "|".join(CALL_ARGS) + r")\s*\(")


def page_script(html_text: str) -> str:
    m = re.search(r"<script>(.*?)</script>", html_text, re.S)
    assert m, "index.html must contain exactly one <script> block"
    return m.group(1)


def _match_paren(src: str, open_idx: int) -> int:
    """Index of the ')' closing the '(' at open_idx, skipping string contents."""
    depth, i, n = 0, open_idx, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":  # skip the string (no nested backticks in this page)
            q, i = c, i + 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    break
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError("unbalanced parens in page script")


def _split_args(inner: str) -> list[str]:
    """Split a call's inner text on top-level commas."""
    args, buf, depth, i, n = [], [], 0, 0, len(inner)
    while i < n:
        c = inner[i]
        if c in "\"'`":
            q = c
            buf.append(c)
            i += 1
            while i < n:
                buf.append(inner[i])
                if inner[i] == "\\":
                    buf.append(inner[i + 1])
                    i += 2
                    continue
                if inner[i] == q:
                    break
                i += 1
        elif c in "([{":
            depth += 1
            buf.append(c)
        elif c in ")]}":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    args.append("".join(buf))
    return args


def normalize_path(arg: str):
    """Page path expression -> canonical path template, or None if the path
    is a runtime expression we cannot resolve statically."""
    arg = arg.strip()
    m = re.fullmatch(r"`([^`]*)`", arg, re.S)
    if m:  # template literal: every ${...} is one path param
        lit = re.sub(r"\$\{[^}]*\}", "{p}", m[1])
    else:
        m = re.match(r"(['\"])(.*?)\1(.*)$", arg, re.S)
        if not m:
            return None
        lit, rest = m[2], m[3]
        # "/api/x/" + pid (plain identifier) = one more path param;
        # anything after a '?' is query, not path
        if "?" not in lit and re.fullmatch(r"\s*\+\s*[\w.$]+\s*", rest):
            lit += "{p}"
    return lit.split("?")[0].rstrip()


def page_calls() -> tuple[list, list]:
    """([ (method, path-template, line), ... ], [ (fn, snippet, line), ... ])"
    for statically resolvable calls and for dynamic-path calls."""
    src = page_script(PAGE.read_text(encoding="utf-8"))
    calls, dynamic = [], []
    for m in CALL_RE.finditer(src):
        fn = m.group(1)
        path_i, opts_i = CALL_ARGS[fn]
        inner = src[m.end():_match_paren(src, m.end() - 1)]
        args = _split_args(inner)
        line = src.count("\n", 0, m.start()) + 1
        if len(args) <= path_i:
            continue
        path = normalize_path(args[path_i])
        if path is None:
            dynamic.append((fn, args[path_i].strip(), line))
            continue
        opts = args[opts_i] if len(args) > opts_i else ""
        mm = re.search(r"""method\s*:\s*['"](\w+)['"]""", opts)
        calls.append(((mm.group(1).upper() if mm else "GET"), path, line))
    return calls, dynamic


def registered_routes(tmp_path) -> set:
    """{(METHOD, canonical path), ...} as create_app registers them."""
    app = create_app(tmp_path / "hubdata")
    out = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            canon = re.sub(r"\{[^}]+\}", "{p}", route.path)
            for method in route.methods:
                out.add((method, canon))
    return out


def key(path: str) -> str:
    return "/".join("{p}" if seg == "{p}" else seg for seg in path.split("/"))


def test_page_script_extracts_a_meaningful_number_of_calls():
    calls, dynamic = page_calls()
    assert len(calls) >= 20, f"only {len(calls)} static calls extracted — regex broken?"
    # exactly the calls with a runtime path (chat audio_url + the plumbing helpers)
    assert {fn for fn, _, _ in dynamic} <= {"api", "fetch", "audioBlobUrl", "playInto"}


def test_every_page_call_matches_a_registered_route(tmp_path):
    """The regression: a GET/POST mismatch (like the old GET /voice/preview
    405) shows up here as a (method, path) pair with no matching route."""
    routes = registered_routes(tmp_path)
    bad = [(method, path, line) for method, path, line in page_calls()[0]
           if (method, path) not in routes]
    assert not bad, "page calls routes the hub does not register:\n" + "\n".join(
        f"  {m} {p}  (index.html script line {ln})" for m, p, ln in bad)


def test_voice_preview_is_fetched_with_post():
    """The original bug, spelled out: Preview must POST, never GET."""
    calls, _ = page_calls()
    preview = {(m, p) for m, p, _ in calls if p.endswith("/voice/preview")}
    assert ("POST", "/api/personas/{p}/voice/preview") in preview
    assert ("GET", "/api/personas/{p}/voice/preview") not in preview


def test_history_tab_has_selection_controls():
    """The select + delete / remember UI exists and wires to the per-message
    routes (single delete, bulk delete, remember, 'load older' paging)."""
    html = PAGE.read_text(encoding="utf-8")
    script = page_script(html)
    for el in ("hselall", "hdelsel", "hshort", "holder"):
        assert f'id="{el}"' in html, f"history toolbar control {el} missing"
    for token in ("hmenu", "hdelone", "hmem", "hsel", "Load older",
                  "Delete selected", "Add to memory", "show only empty/short"):
        assert token in html, f"history UI token {token!r} missing"
    calls, _ = page_calls()
    pairs = {(m, p) for m, p, _ in calls}
    assert ("POST", "/api/history/delete") in pairs
    assert ("DELETE", "/api/history/{p}") in pairs
    assert ("POST", "/api/history/{p}/remember") in pairs
    assert "before=" in script          # 'load older' paging param
    assert "history_changed" in script  # live refresh on deletes
