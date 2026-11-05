"""Read-only reader for the owner's Hyprland Lua config (contract §1).

hyprland.lua FIRST, then its require("…") / dofile / loadfile lines
(module names resolve relative to ~/.config/hypr/), and parses — never
writes, never reloads:

- hl.monitor({output=…, mode=…, position=…, scale=…}) blocks, including
  `desc:` outputs; commented-out blocks are ignored (comments are stripped
  string-aware first, so `--` inside a URL string survives);
- hl.workspace_rule({workspace=…, monitor=…}) with local-variable
  resolution and `for i = a, b do … tostring(i) … end` expansion (the
  owner's real config uses both);
- app-launching binds: hl.bind(…, hl.dsp.exec_cmd("kitty")) plus hl.env
  hints (TERMINAL=…) → a preferred {terminal, browser, filemanager} map.

A minimal classic hyprland.conf fallback (monitor=/workspace=/bind=/env=/
source=) keeps non-Lua setups working.
"""
from __future__ import annotations

import re
from pathlib import Path

_MAX_DEPTH = 8


# ── comment stripping (string-aware) ───────────────────────────
def strip_lua_comments(src: str) -> str:
    out = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == ch:
                    break
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
            continue
        if re.match(r"\[=*\[", src[i:]):
            m = re.match(r"\[(=*)\[", src[i:])
            close = src.find("]" + m.group(1) + "]", i + len(m.group(0)))
            close = n - 1 if close < 0 else close + len(m.group(1)) + 1
            out.append(src[i:close + 1])
            i = close + 1
            continue
        if src.startswith("--", i):
            if src.startswith("--[", i):  # possible --[[ long comment ]]
                m = re.match(r"--\[(=*)\[", src[i:])
                if m:
                    close = src.find("]" + m.group(1) + "]", i + len(m.group(0)))
                    i = n if close < 0 else close + len(m.group(0)) - 1
                    continue
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ── mini Lua value parser (table literals inside hl.* calls) ───
_TOK = re.compile(r"""
    (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op>[{}\[\](),=])
  | (?P<ws>\s+)""", re.VERBOSE)


def _tokenize(s):
    pos, toks = 0, []
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m:
            pos += 1  # unknown char: skip (comments were stripped already)
            continue
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        toks.append((kind, m.group()))
    return toks


def _lua_string(tok: str) -> str:
    body = tok[1:-1]
    return re.sub(r"\\([ntr\"'\\])", lambda m: {"n": "\n", "t": "\t", "r": "\r"}
                  .get(m.group(1), m.group(1)), body)


class _TableParser:
    def __init__(self, toks, variables):
        self.t, self.i, self.vars = toks, 0, variables

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def parse_value(self):
        kind, text = self.peek()
        self.i += 1
        if kind == "str":
            return _lua_string(text)
        if kind == "num":
            return float(text) if "." in text else int(text)
        if kind == "name":
            if text in ("true", "false"):
                return text == "true"
            if text == "tostring":       # tostring(N) after loop expansion
                if self.peek() == ("op", "("):
                    self.i += 1
                    v = self.parse_value()
                    if self.peek() == ("op", ")"):
                        self.i += 1
                    return str(v)
            # variable reference: resolved via collected locals/globals
            return self.vars.get(text, text)
        if kind == "op" and text == "{":
            return self.parse_table()
        return None

    def parse_table(self):
        out, seq = {}, []
        while True:
            kind, text = self.peek()
            if kind is None:
                break
            if kind == "op" and text == "}":
                self.i += 1
                break
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else (None, None)
            if kind in ("name", "str") and nxt == ("op", "="):
                self.i += 2
                out[_lua_string(text) if kind == "str" else text] = self.parse_value()
            else:
                seq.append(self.parse_value())
            kind, text = self.peek()
            if kind == "op" and text in (",", ";"):
                self.i += 1
        if seq and not out:
            return seq
        if seq:
            out["__list"] = seq
        return out


def parse_lua_table(src: str, variables=None) -> dict:
    toks = _tokenize(src)
    return _TableParser(toks, variables or {}).parse_table()


def _call_bodies(text: str, fname: str):
    """Every `fname( ... )` balanced-paren body (string-aware)."""
    for m in re.finditer(re.escape(fname) + r"\s*\(", text):
        i = m.end()
        depth, start, n = 1, i, len(text)
        while i < n and depth:
            c = text[i]
            if c in "\"'":
                j = i + 1
                while j < n and text[j] != c:
                    j += 2 if text[j] == "\\" else 1
                i = j + 1
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            i += 1
        yield text[start:i - 1]


# ── variable + loop pre-processing ─────────────────────────────
_ASSIGN = re.compile(r"^\s*(?:local\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*"
                     r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|-?\d+(?:\.\d+)?|true|false)\s*$",
                     re.MULTILINE)
_FOR = re.compile(r"for\s+([A-Za-z_]\w*)\s*=\s*(-?\d+)\s*,\s*(-?\d+)\s+do(.*?)\bend\b",
                  re.DOTALL)


def expand(text: str, variables: dict) -> str:
    """Collect simple scalar assignments into `variables` and inline for
    loops so `tostring(i)` becomes literal numbers before table parsing."""
    for m in _ASSIGN.finditer(text):
        name, val = m.group(1), m.group(2)
        if val[0] in "\"'":
            variables[name] = _lua_string(val)
        elif val in ("true", "false"):
            variables[name] = val == "true"
        else:
            variables[name] = float(val) if "." in val else int(val)

    def unroll(m):
        var, a, b, body = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        parts = []
        for k in range(a, b + 1):
            sub = re.sub(r"tostring\(\s*" + re.escape(var) + r"\s*\)", repr(k), body)
            sub = re.sub(r"\b" + re.escape(var) + r"\b", repr(k), sub)
            parts.append(sub)
        return "\n".join(parts)

    return _FOR.sub(unroll, text)


# ── the reader ─────────────────────────────────────────────────
def _resolve_module(base: Path, spec: str, kind: str) -> Path | None:
    if kind == "require":
        rel = spec.replace(".", "/")
        cand = base / (rel + ".lua")
        if not cand.is_file():
            cand = base / rel / "init.lua"
        return cand if cand.is_file() else None
    p = Path(spec)
    if not p.is_absolute():
        p = base / spec
    return p if p.is_file() else None


class HyprLua:
    """Parsed view of the owner's Lua config (read-only)."""

    def __init__(self, config_dir=None, entry="hyprland.lua"):
        base = Path(config_dir or (Path.home() / ".config" / "hypr"))
        self.dir = base
        self.monitors: list[dict] = []
        self.workspace_rules: list[dict] = []
        self.exec_binds: list[str] = []
        self.env: dict[str, str] = {}
        self.preferred: dict[str, str] = {}
        self.files_read: list[str] = []
        if (base / entry).is_file():
            self._read_lua(base / entry, set(), 0)
        elif (base / "hyprland.conf").is_file():
            self._read_conf(base / "hyprland.conf", set(), 0)
        self._derive_preferred()

    # lua pass
    def _read_lua(self, path: Path, seen: set, depth: int):
        if depth > _MAX_DEPTH or path in seen or not path.is_file():
            return
        seen.add(path)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self.files_read.append(str(path))
        text = strip_lua_comments(raw)
        vars_: dict = {}
        text = expand(text, vars_)
        self.env.update({k: str(v) for k, v in vars_.items()})
        for body in _call_bodies(text, "hl.monitor"):
            t = parse_lua_table(body, vars_)
            if isinstance(t, dict) and t.get("output"):
                self.monitors.append(t)
        for body in _call_bodies(text, "hl.workspace_rule"):
            t = parse_lua_table(body, vars_)
            if isinstance(t, dict) and t.get("workspace"):
                self.workspace_rules.append(t)
        for body in _call_bodies(text, "hl.env"):
            t = _TableParser(_tokenize(body), vars_)
            vals = [t.parse_value() for _ in range(2) if t.peek()[0]]
            if len(vals) == 2 and vals[0]:
                self.env[str(vals[0])] = str(vals[1])
        # keybinds that launch apps: hl.bind(<keys>, hl.dsp.exec_cmd("…"))
        for line in text.splitlines():
            m = re.search(r"hl\.bind\(.*?exec_cmd\(\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", line)
            if m:
                self.exec_binds.append(_lua_string(m.group(1)))
        # follow require/dofile/loadfile (relative to this file's dir)
        for m in re.finditer(r"\b(require|dofile|loadfile)\s*\(\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", text):
            spec = _lua_string(m.group(2))
            nxt = _resolve_module(path.parent, spec, m.group(1))
            if nxt:
                self._read_lua(nxt, seen, depth + 1)

    # classic conf fallback (kept small)
    def _read_conf(self, path: Path, seen: set, depth: int):
        if depth > _MAX_DEPTH or path in seen or not path.is_file():
            return
        seen.add(path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        self.files_read.append(str(path))
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, rest = line.partition("=")
            key, rest = key.strip().lower(), rest.strip()
            parts = [p.strip() for p in rest.split(",")]
            if key == "monitor" and parts:
                out = parts[0]
                mode = parts[1] if len(parts) > 1 else "preferred"
                pos = parts[2] if len(parts) > 2 else "auto"
                scale = parts[3] if len(parts) > 3 else "1"
                self.monitors.append({"output": out, "mode": mode,
                                      "position": pos, "scale": scale})
            elif key == "workspace" and len(parts) >= 2:
                self.workspace_rules.append({"workspace": parts[0],
                                             "monitor": parts[1].replace("monitor:", "")})
            elif key in ("exec", "exec-once", "bind") :
                if key == "bind":
                    if len(parts) >= 4 and parts[2].strip() == "exec":
                        self.exec_binds.append(",".join(parts[3:]))
                else:
                    self.env.setdefault("__exec__", "")
                    self.exec_binds.append(rest) if key == "exec" else None
            elif key == "env" and len(parts) >= 2:
                self.env[parts[0]] = parts[1]
            elif key == "source" and parts:
                p = Path(parts[0]).expanduser()
                if not p.is_absolute():
                    p = path.parent / parts[0]
                self._read_conf(p, seen, depth + 1)

    def _derive_preferred(self):
        from .apps import _PREFERRED_HINTS
        hay = " ".join(self.exec_binds + [self.env.get("TERMINAL", ""),
                                          self.env.get("BROWSER", "")]).lower()
        words = set(re.split(r"[^a-z0-9_.-]+", hay))
        for role, hints in _PREFERRED_HINTS.items():
            for h in hints:
                if h in hay:
                    self.preferred[role] = h
                    break
        term = self.env.get("TERMINAL")
        if term:
            self.preferred["terminal"] = str(term).strip()

    def configured_monitors(self) -> list[dict]:
        """The hl.monitor blocks (the owner's INTENT; live state wins)."""
        out = []
        for m in self.monitors:
            out.append({
                "output": str(m.get("output") or ""),
                "mode": str(m.get("mode") or ""),
                "position": str(m.get("position") or ""),
                "scale": m.get("scale"),
            })
        return out
