"""Obsidian Local REST API client + tools (docs/TYPING.md, Obsidian section).

Speaks to the owner-installed community plugin "Local REST API"
(coddingtonbear) on https://127.0.0.1:27124 — routes per its README /
OpenAPI (checked 2026-11-09 against the current plugin):

  GET    /                       status (reachable + authenticated)
  GET    /vault/{path}           read a file (ETag header = version)
  PUT    /vault/{path}           create/replace  (204)
  POST   /vault/{path}           append          (204)
  PATCH  /vault/{path}/heading/{H}  targeted insert with `Operation:` header
  DELETE /vault/{path}           delete
  GET    /active/                the file open in Obsidian (Content-Location
                                 names the resolved vault path)
  POST   /active/                append to it
  POST   /open/{path}?newLeaf=   open a file in the UI
  POST   /search/simple/?query=  full-text search
  GET    /obsidian-local-rest-api.crt   the plugin's own CA certificate

Certificate choice: the plugin signs itself from a locally generated CA
served at /obsidian-local-rest-api.crt. We **pin that CA**: fetched once
over an unverified connection (it is loopback-only — the URL host is
hard-checked below), cached at ~/.config/riko/obsidian_ca.pem (0600),
and every later request verifies the server certificate against the
cached copy. A rotated cert then fails loudly rather than silently; the
error says to delete the cached file if the change is expected. An
`"insecure": true` entry in the config keeps plain loopback verification
off (documented escape hatch; still 127.0.0.1/localhost only). `http://`
base URLs (the plugin's optional 27123 server, and the test suite) skip
TLS entirely.

The API key lives in ~/.config/riko/obsidian.json (0600, written by the
tray "Obsidian…" dialog or `riko-ctl obsidian-key`). It is never logged,
never echoed in an error, and never sent anywhere but 127.0.0.1.

Approval note: obsidian_edit runs its own confirmation through the
runtime (NeedConfirmation) so the owner sees a real diff; its hub
permission default is therefore `always` — the diff prompt IS the ask.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from riko.client_tools import NeedConfirmation, ToolError, config_dir

DEFAULT_BASE_URL = "https://127.0.0.1:27124"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
READ_CAP = 20_000          # obsidian_read default, chars
_SETUP_HINT = ("is Obsidian running with the community plugin 'Local REST "
               "API' enabled? (Settings → Community plugins → Installed → "
               "Local REST API → show key)")


# ── settings / key ─────────────────────────────────────────────
def settings_path() -> Path:
    return Path(os.environ.get("RIKO_OBSIDIAN_CONFIG")
                or config_dir() / "obsidian.json")


def ca_path() -> Path:
    return settings_path().with_name("obsidian_ca.pem")


def load_settings() -> dict:
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_key(key, base_url=None) -> Path:
    """Store the API key (mode 0600). Never call with the key in a log
    line: this function is the only place the value is written."""
    key = str(key or "").strip()
    if not key:
        raise ToolError("the Obsidian key is empty — copy it from Obsidian's "
                        "Local REST API settings")
    path = settings_path()
    cfg = load_settings()
    cfg["key"] = key
    if base_url:
        cfg["base_url"] = str(base_url).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def key_is_set() -> bool:
    return bool(load_settings().get("key"))


# ── transport ──────────────────────────────────────────────────
def _quote_vault_path(path) -> str:
    parts = [urllib.parse.quote(p, safe="")
             for p in str(path or "").strip().strip("/").split("/") if p]
    return "/".join(parts)


class ObsidianRest:
    """Thin urllib client for one vault. Tests point base_url at a fake
    http:// server on a scratch port; the owner's runs use the default
    https://127.0.0.1:27124 with the pinned CA."""

    def __init__(self, key=None, base_url=None, insecure=None,
                 ca_file=None, timeout=15.0):
        cfg = load_settings()
        self.key = str(key if key is not None else cfg.get("key") or "")
        base = str(base_url or os.environ.get("RIKO_OBSIDIAN_URL")
                   or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
            raise ToolError(f"Obsidian URL must be http(s) on loopback, got {base!r}")
        self.base_url = base
        self.insecure = bool(insecure if insecure is not None
                             else cfg.get("insecure"))
        self.ca_file = Path(ca_file) if ca_file else ca_path()
        self.timeout = float(timeout)

    # TLS
    def _context(self):
        if self.base_url.startswith("http://"):
            return None
        if self.insecure:
            ctx = ssl._create_unverified_context()
            return ctx
        if not self.ca_file.exists():
            self._fetch_ca()
        ctx = ssl.create_default_context(cafile=str(self.ca_file))
        return ctx

    def _fetch_ca(self):
        """One-time download of the plugin's CA over loopback. Trusting an
        unverified connection here is the documented pin-on-first-use
        choice; the host is already hard-checked to 127.0.0.1."""
        url = f"{self.base_url}/obsidian-local-rest-api.crt"
        try:
            with urllib.request.urlopen(
                    url, timeout=self.timeout,
                    context=ssl._create_unverified_context()) as r:
                pem = r.read()
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"can't reach Obsidian's certificate at {url}: {e}. "
                            f"{_SETUP_HINT}") from None
        self.ca_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.ca_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        try:
            os.chmod(self.ca_file, 0o600)
        except OSError:
            pass

    # core request
    def request(self, method, route, *, query=None, body=None,
                content_type: "str | None" = "text/markdown", headers=None):
        """One API call. Returns (status, headers, body_bytes).
        Maps failures to clear ToolErrors; never includes the key."""
        url = self.base_url + route
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None})
        hdrs = dict(headers or {})
        if self.key:
            hdrs["Authorization"] = f"Bearer {self.key}"
        data = None
        if body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
            if content_type:
                hdrs["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._context()) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            payload = b""
            try:
                payload = e.read()
            except Exception:
                pass
            raise self._http_error(e.code, route, payload) from None
        except ssl.SSLCertVerificationError as e:
            raise ToolError(
                f"Obsidian's certificate didn't match the one I pinned "
                f"({e.verify_message if hasattr(e, 'verify_message') else e}). "
                f"If you reinstalled the plugin, delete "
                f"{self.ca_file} and try again so I can re-pin it.") from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ToolError(f"can't reach Obsidian at {self.base_url}: {reason}. "
                            f"{_SETUP_HINT}") from None

    def _http_error(self, code, route, payload):
        snippet = ""
        try:
            snippet = str(json.loads(payload).get("message") or "")[:200]
        except (ValueError, AttributeError):
            snippet = (payload or b"").decode("utf-8", "replace")[:200]
        if code == 401:
            return ToolError(
                "Obsidian rejected the API key (401). Re-copy it: Obsidian "
                "settings → Local REST API → API key, then the Riko tray "
                "'Obsidian…' or `riko-ctl obsidian-key <key>`.")
        if code == 403:
            return ToolError(f"Obsidian refused that request (403): {snippet or route}")
        if code == 404:
            return ToolError(f"Obsidian has no such file: {route} (404)")
        return ToolError(f"Obsidian answered {code} for {route}: "
                         f"{snippet or 'no message'}")

    # endpoints
    def status(self):
        status, _h, body = self.request("GET", "/")
        try:
            out = json.loads(body)
        except ValueError:
            out = {}
        return out if isinstance(out, dict) else {}

    def open_path(self, path, new_leaf=False):
        self.request("POST", f"/open/{_quote_vault_path(path)}",
                     query={"newLeaf": "true" if new_leaf else None})

    def read(self, path):
        _s, h, body = self.request("GET", f"/vault/{_quote_vault_path(path)}")
        return body.decode("utf-8", "replace"), h.get("ETag")

    def read_active(self):
        _s, h, body = self.request("GET", "/active/")
        loc = urllib.parse.unquote(h.get("Content-Location") or "")
        shown = loc[len("/vault/"):] if loc.startswith("/vault/") else loc
        return body.decode("utf-8", "replace"), shown

    def put(self, path, text):
        self.request("PUT", f"/vault/{_quote_vault_path(path)}", body=text)

    def put_active(self, text):
        self.request("PUT", "/active/", body=text)

    def append(self, path, text):
        if path:
            self.request("POST", f"/vault/{_quote_vault_path(path)}", body=text)
            return str(path)
        _s, h, _b = self.request("POST", "/active/", body=text)
        loc = urllib.parse.unquote(h.get("Content-Location") or "")
        return loc[len("/vault/"):] if loc.startswith("/vault/") else "(active file)"

    def patch_under_heading(self, path, heading, content, operation="append"):
        """Targeted insert via PATCH raw-content mode: URL carries
        /heading/<segment>/<segment>, the body is the raw markdown, and
        `Operation:` picks append|prepend|replace. (The old Target-Type /
        Target header form is deprecated plugin-side and needs
        Markdown-Patch-Version: 1 — we use the current form.)"""
        segs = [str(h).strip() for h in str(heading).split(">") if str(h).strip()]
        if not segs:
            raise ToolError("obsidian_insert: give a heading, e.g. heading='Log' "
                            "or heading='Work > Meetings'")
        target = "/".join(urllib.parse.quote(s, safe="") for s in segs)
        self.request("PATCH", f"/vault/{_quote_vault_path(path)}/heading/{target}",
                     body=content, headers={"Operation": operation})

    def search(self, query, max_results=8, context_length=100):
        _s, _h, body = self.request(
            "POST", "/search/simple/", body=b"",
            content_type=None,
            query={"query": str(query), "maxResults": int(max_results),
                   "contextLength": int(context_length)})
        try:
            rows = json.loads(body)
        except ValueError:
            rows = []
        return rows if isinstance(rows, list) else []


# ── default client (lazy, config-driven) ──────────────────────
_default: dict = {"client": None}


def _client() -> ObsidianRest:
    c = _default["client"]
    if c is None:
        c = ObsidianRest()
        _default["client"] = c
    return c


def set_client(client):
    """Tests inject a client aimed at the fake server; None resets."""
    _default["client"] = client


def _need_key():
    c = _client()
    if not c.key:
        raise ToolError("no Obsidian API key saved yet — copy it from "
                        "Obsidian's Local REST API settings into Riko's tray "
                        "'Obsidian…' dialog or `riko-ctl obsidian-key <key>`.")
    return c


# ── tool implementations ───────────────────────────────────────
def obsidian_status(**_kw):
    out: dict = {"reachable": False}
    try:
        st = _client().status()
    except ToolError as e:
        out["error"] = str(e)
        return out
    out["reachable"] = True
    out["authenticated"] = bool(st.get("authenticated"))
    if st.get("versions"):
        out["versions"] = st["versions"]
    if out["authenticated"]:
        try:
            _text, shown = _client().read_active()
            if shown:
                out["active_file"] = shown
        except ToolError:
            pass
    return out


def obsidian_open(path, new_leaf=False, **_kw):
    path = str(path or "").strip()
    if not path:
        raise ToolError("obsidian_open: which note?")
    _need_key().open_path(path, new_leaf=bool(new_leaf))
    return f"opened {path} in Obsidian" + (" (new leaf)" if new_leaf else "")


def obsidian_create(path, text="", overwrite=False, **_kw):
    path = str(path or "").strip()
    if not path:
        raise ToolError("obsidian_create: which path in the vault?")
    c = _need_key()
    try:
        c.read(path)
        exists = True
    except ToolError as e:
        if "404" not in str(e):
            raise
        exists = False
    if exists and not overwrite:
        raise ToolError(f"{path} already exists — I won't overwrite it. "
                        f"obsidian_append or obsidian_edit are gentler; "
                        f"pass overwrite=true to replace the whole note.")
    c.put(path, str(text or ""))
    verb = "replaced" if exists else "created"
    return f"{verb} {path} ({len(str(text or ''))} chars)"


def obsidian_read(path=None, max_chars=READ_CAP, **_kw):
    c = _need_key()
    cap = max(200, min(int(max_chars or READ_CAP), READ_CAP))
    if path:
        text, _etag = c.read(str(path).strip())
        shown = str(path).strip()
    else:
        text, shown = c.read_active()
    truncated = len(text) > cap
    return {"path": shown, "text": text[:cap] + ("\n… (truncated)" if truncated else ""),
            "truncated": truncated, "chars": len(text)}


def obsidian_append(text, path=None, **_kw):
    if not str(text or ""):
        raise ToolError("obsidian_append: nothing to append")
    shown = _need_key().append(str(path).strip() if path else None, str(text))
    return f"appended {len(str(text))} chars to {shown}"


def _resolve_target(c, path):
    if path:
        text, _etag = c.read(str(path).strip())
        return text, str(path).strip()
    return c.read_active()


def obsidian_edit(path=None, find=None, replace=None, confirmed=False, **_kw):
    c = _need_key()
    text, shown = _resolve_target(c, path)
    shown = shown or "(active file)"
    from .files import apply_edit_once, compact_diff
    new = apply_edit_once(text, find, replace)
    if not confirmed:
        raise NeedConfirmation(
            f"edit Obsidian note {shown}:\n{compact_diff(text, new)}",
            "obsidian_edit",
            {"path": path, "find": find, "replace": replace})
    if path:
        c.put(shown, new)
    else:
        c.put_active(new)
    return f"edited {shown}: replaced 1 occurrence of {len(str(find))} chars " \
           f"with {len(str(replace or ''))}"


def obsidian_insert(path, heading, text, operation="append", **_kw):
    if str(operation) not in ("append", "prepend", "replace"):
        raise ToolError("obsidian_insert: operation is append | prepend | replace")
    if not str(text or ""):
        raise ToolError("obsidian_insert: nothing to insert")
    _need_key().patch_under_heading(str(path).strip(), heading, str(text),
                                    operation=str(operation))
    return f"{operation}ed {len(str(text))} chars under heading {heading!r} in {path}"


def obsidian_search(query, max_results=8, **_kw):
    if not str(query or "").strip():
        raise ToolError("obsidian_search: give a query")
    rows = _need_key().search(str(query).strip(), max_results=int(max_results or 8))
    out = []
    for r in rows[:12]:
        if not isinstance(r, dict):
            continue
        out.append({"file": r.get("filename"),
                    "matches": r.get("totalMatches", len(r.get("matches") or [])),
                    "snippet": str(r.get("match") or r.get("result") or "")[:180]})
    return {"query": query, "results": out}


# ── catalogue ──────────────────────────────────────────────────
def _obj(props, required=None):
    spec = {"type": "object", "additionalProperties": False,
            "properties": props}
    if required:
        spec["required"] = list(required)
    return spec


def obsidian_tools():
    return [
        {"name": "obsidian_status", "risk": "read",
         "description": "Is the owner's Obsidian reachable through the Local "
                        "REST API plugin, and is our key accepted? Also the "
                        "file currently open in Obsidian.",
         "parameters": _obj({})},
        {"name": "obsidian_open", "risk": "read",
         "description": "Open a vault note in the Obsidian window.",
         "parameters": _obj({"path": {"type": "string",
                                      "description": "vault-relative path, "
                                                     "e.g. 'Ideas/shop.md'"},
                             "new_leaf": {"type": "boolean"}})},
        {"name": "obsidian_create", "risk": "act",
         "description": "Create a new vault note (refuses an existing one "
                        "unless overwrite=true, which asks the owner).",
         "parameters": _obj({"path": {"type": "string"},
                             "text": {"type": "string"},
                             "overwrite": {"type": "boolean"}},
                            required=["path"])},
        {"name": "obsidian_read", "risk": "read",
         "description": "Read a vault note (or the one open in Obsidian when "
                        "path is omitted). Text is capped.",
         "parameters": _obj({"path": {"type": "string"},
                             "max_chars": {"type": "integer"}})},
        {"name": "obsidian_append", "risk": "act",
         "description": "Append text to a vault note (or the active file when "
                        "path is omitted).",
         "parameters": _obj({"text": {"type": "string"},
                             "path": {"type": "string"}},
                            required=["text"])},
        {"name": "obsidian_edit", "risk": "act", "permission_default": "always",
         "description": "Exact-match edit in a vault note: `find` must occur "
                        "exactly once or nothing changes. The owner sees a "
                        "diff before it is applied. Path omitted = the file "
                        "open in Obsidian.",
         "parameters": _obj({"path": {"type": "string"},
                             "find": {"type": "string"},
                             "replace": {"type": "string"}},
                            required=["find", "replace"])},
        {"name": "obsidian_insert", "risk": "act",
         "description": "Insert text under a heading in a note via the "
                        "plugin's targeted PATCH (operation append|prepend|"
                        "replace; 'A > B' addresses nested headings).",
         "parameters": _obj({"path": {"type": "string"},
                             "heading": {"type": "string"},
                             "text": {"type": "string"},
                             "operation": {"type": "string",
                                           "enum": ["append", "prepend",
                                                    "replace"]}},
                            required=["path", "heading", "text"])},
        {"name": "obsidian_search", "risk": "read",
         "description": "Full-text search the vault (Obsidian's own fuzzy "
                        "search); returns files with scored snippets.",
         "parameters": _obj({"query": {"type": "string"},
                             "max_results": {"type": "integer"}},
                            required=["query"])},
    ]


def obsidian_executors():
    return {t["name"]: globals()[t["name"]] for t in obsidian_tools()}
