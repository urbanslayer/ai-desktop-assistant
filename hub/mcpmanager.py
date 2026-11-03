"""MCP client side of the hub (docs/ROUNDS.md R2-M, docs/API.md).

Every enabled server in settings `mcp.servers` gets a dedicated asyncio task
on a private loop thread: connect (stdio subprocess or streamable HTTP),
initialize, list tools, then hold the session open. A dead session reconnects
with exponential backoff; the restart route pokes it to reconnect now. Tool
results, logs and events all flow through the existing tool loop — an MCP
tool is a plain ToolSpec whose `run` marshals one call onto this loop.

The official `mcp` SDK is imported lazily: a hub started without the package
(e.g. local mode where the root project forgot the dep) still boots, and every
server reports state "unavailable: mcp package not installed".

Model-facing names: `mcp_<server>__<tool>`, sanitized to ^[A-Za-z0-9_-]{1,64}$.
Risk: readOnlyHint -> read, destructiveHint -> danger, everything else act.
"""
import asyncio
import hashlib
import json
import os
import re
import threading
import time

try:
    from .tools.registry import ToolSpec
    from .store import now_iso
except (ImportError, SystemError):  # standalone: hub/ is on sys.path
    from tools.registry import ToolSpec  # type: ignore[no-redef]
    from store import now_iso  # type: ignore[no-redef]

SERVER_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRANSPORTS = ("stdio", "http")
PERM_VALUES = ("always", "ask", "never")
UNAVAILABLE_MSG = "mcp package not installed"
MAX_TOOL_RESULT_CHARS = 100_000  # pre-cap for blocks; the tool loop caps again
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
WATCHDOG_S = 2.0                 # ping cadence once ready (detect a dead server)
PING_TIMEOUT_S = 10.0
DEFAULT_CALL_TIMEOUT_S = 60

# ── lazy SDK import ──────────────────────────────────────────────────────
_SDK: dict | None = None
_SDK_FAILED: Exception | None = None


def sdk_error() -> str:
    """"" when the SDK imports, else why it does not."""
    load_sdk()
    return "" if _SDK is not None else f"{type(_SDK_FAILED).__name__}: {_SDK_FAILED}"


def sdk_available() -> bool:
    return load_sdk() is not None


def load_sdk() -> dict | None:
    """Import the mcp client surface once; {}-ish dict of names or None."""
    global _SDK, _SDK_FAILED
    if _SDK is not None or _SDK_FAILED is not None:
        return _SDK
    try:
        import mcp  # noqa: F401
        from mcp import Client, StdioServerParameters
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        from mcp.types import ToolAnnotations  # noqa: F401  (shape check only)
        _SDK = {"Client": Client, "StdioServerParameters": StdioServerParameters,
                "streamable_http_client": streamable_http_client,
                "create_mcp_http_client": create_mcp_http_client}
    except Exception as e:  # ImportError, or a broken install
        _SDK_FAILED = e
    return _SDK


# ── settings validation (used by PUT /api/settings) ──────────────────────

def validate_servers(raw_servers, previous: list | None) -> list[dict]:
    """Clean the `mcp.servers` list from a PUT body. `previous` is the
    currently saved list — env/header values are write-only, and an empty
    string for a key the previous server already had keeps the old value
    (same rule as llm.api_key); omitting a key removes it."""
    if not isinstance(raw_servers, list):
        raise ValueError("mcp.servers must be a list")
    prev = {s.get("name"): s for s in (previous or []) if isinstance(s, dict)}
    out: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_servers):
        if not isinstance(raw, dict):
            raise ValueError(f"mcp.servers[{i}] must be an object")
        name = str(raw.get("name") or "").strip().lower()
        if not SERVER_NAME_RE.match(name):
            raise ValueError(f"server name '{name}' must match ^[a-z0-9_-]{{1,32}}$")
        if name in seen:
            raise ValueError(f"duplicate server name '{name}'")
        seen.add(name)
        transport = str(raw.get("transport") or "stdio").strip().lower()
        if transport not in TRANSPORTS:
            raise ValueError(f"server '{name}' transport must be stdio or http")
        srv: dict = {"name": name, "transport": transport,
                     "enabled": raw.get("enabled") is not False}
        if transport == "stdio":
            command = str(raw.get("command") or "").strip()
            if not command:
                raise ValueError(f"server '{name}' (stdio) needs a command")
            srv["command"] = command[:300]
            args = raw.get("args") or []
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError(f"server '{name}' args must be a list of strings")
            srv["args"] = [a[:1000] for a in args[:64]]
        else:
            url = str(raw.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"server '{name}' url must start with http:// or https://")
            srv["url"] = url[:500]
        for key, prev_key in (("env", "env"), ("headers", "headers")):
            if key in raw:
                if not isinstance(raw[key], dict):
                    raise ValueError(f"server '{name}' {key} must be an object")
                old_vals = (prev.get(name) or {}).get(prev_key) or {}
                cleaned: dict[str, str] = {}
                for k, v in raw[key].items():
                    k = str(k).strip()
                    if not k:
                        continue
                    v = "" if v is None else str(v)
                    if not v and old_vals.get(k):
                        v = str(old_vals[k])  # write-only: blank keeps the secret
                    cleaned[k[:100]] = v[:2000]
                srv[key] = cleaned
        tp = raw.get("tool_permissions")
        if tp is not None:
            if not isinstance(tp, dict):
                raise ValueError(f"server '{name}' tool_permissions must be an object")
            perms: dict[str, str] = {}
            for tname, perm in tp.items():
                if perm is None:
                    continue
                if str(perm) not in PERM_VALUES:
                    raise ValueError(f"permission for '{tname}' must be always, ask, "
                                     "never or null")
                perms[str(tname)[:80]] = str(perm)
            srv["tool_permissions"] = perms
        if "timeout" in raw:
            try:
                num = int(raw["timeout"])
            except (TypeError, ValueError):
                raise ValueError(f"server '{name}' timeout must be an integer")
            if not 1 <= num <= 600:
                raise ValueError(f"server '{name}' timeout must be between 1 and 600")
            srv["timeout"] = num
        out.append(srv)
    return out


def public_servers(servers: list[dict]) -> list[dict]:
    """GET shape: env/header VALUES are write-only — keys are listed with a
    `true`/`false` "is set" flag instead of the value."""
    out = []
    for s in servers:
        pub = {k: v for k, v in s.items() if k not in ("env", "headers")}
        pub["env"] = {k: bool(v) for k, v in (s.get("env") or {}).items()}
        pub["headers"] = {k: bool(v) for k, v in (s.get("headers") or {}).items()}
        out.append(pub)
    return out


# ── naming / risk ─────────────────────────────────────────────────────────

def model_tool_name(server: str, tool: str) -> str:
    """mcp_<server>__<tool> sanitized to the OpenAI function charset."""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", server)
    t = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    budget = 64 - (len("mcp_") + len(s) + len("__"))
    t = t[:max(1, budget)] or "tool"
    return f"mcp_{s}__{t}"


def risk_for_annotations(annotations) -> str:
    if annotations is not None:
        if getattr(annotations, "read_only_hint", None) is True:
            return "read"
        if getattr(annotations, "destructive_hint", None) is True:
            return "danger"
    return "act"


def result_payload(res) -> dict:
    """CallToolResult -> JSON-ish payload (the tool loop caps and wraps it
    as untrusted data). isError is handled by the caller."""
    texts, blocks = [], []
    for part in getattr(res, "content", None) or []:
        ptype = getattr(part, "type", "?")
        if ptype == "text":
            texts.append(str(getattr(part, "text", "")))
        else:
            blocks.append({"type": ptype})
    payload: dict = {}
    if texts:
        payload["text"] = "\n".join(texts)[:MAX_TOOL_RESULT_CHARS]
    structured = getattr(res, "structured_content", None)
    if structured is not None:
        payload["structured"] = structured
    if blocks:
        payload["content_blocks"] = blocks[:20]
    return payload or {"text": ""}


def _cfg_hash(srv: dict) -> str:
    return hashlib.sha256(json.dumps(srv, sort_keys=True).encode()).hexdigest()


class _Runtime:
    __slots__ = ("cfg", "cfg_hash", "state", "error", "tools", "last_ok",
                 "task", "stop", "attempt", "client", "lock")

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cfg_hash = _cfg_hash(cfg)
        self.state = "disabled" if not cfg.get("enabled") else "starting"
        self.error = ""
        self.tools: list[dict] = []
        self.last_ok = ""
        self.task: asyncio.Task | None = None
        self.stop: asyncio.Event | None = None
        self.attempt = 0
        self.client = None
        self.lock = asyncio.Lock()


class MCPManager:
    """Owns every MCP server session. Blocking methods are called from
    FastAPI threadpool threads and marshal work onto the loop thread."""

    def __init__(self, events=None):
        self.events = events
        self._servers: dict[str, _Runtime] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._spawn_lock = threading.Lock()
        self._shutdown = False

    # ── loop thread ─────────────────────────────────────────────────────
    def _ensure_loop(self) -> bool:
        if self._shutdown:
            return False
        if self._thread is not None:
            return self._ready.wait(5)
        with self._spawn_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._thread_main,
                                                name="mcp-loop", daemon=True)
                self._thread.start()
        return self._ready.wait(5)

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def _submit(self, coro, timeout: float = 15.0):
        if not self._ensure_loop() or self._loop is None:
            raise RuntimeError("MCP loop thread not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # ── config apply ────────────────────────────────────────────────────
    def sync(self, servers: list[dict]) -> None:
        """Apply the saved server list: start new, restart changed, stop
        removed. Quick — connecting happens in the per-server tasks."""
        self._submit(self._sync(list(servers)))

    async def _sync(self, servers: list[dict]) -> None:
        wanted = {s["name"]: s for s in servers}
        for name in list(self._servers):
            if name not in wanted:
                await self._teardown(name)
        for name, cfg in wanted.items():
            rt = self._servers.get(name)
            if rt is not None and rt.cfg_hash == _cfg_hash(cfg):
                continue  # unchanged
            if rt is not None:
                await self._teardown(name)
            rt = _Runtime(cfg)
            self._servers[name] = rt
            if rt.state == "starting":
                rt.task = asyncio.create_task(self._serve(name, rt))

    async def _teardown(self, name: str) -> None:
        rt = self._servers.pop(name, None)
        if rt is None:
            return
        if rt.stop is not None:
            rt.stop.set()
        if rt.task is not None:
            rt.task.cancel()
            try:
                await rt.task
            except (asyncio.CancelledError, Exception):
                pass
        rt.client = None

    # ── per-server lifecycle ────────────────────────────────────────────
    def _build_client(self, rt: _Runtime):
        sdk = load_sdk()
        if sdk is None:
            raise RuntimeError(UNAVAILABLE_MSG)
        cfg = rt.cfg
        if cfg["transport"] == "stdio":
            env = {**os.environ, **{str(k): str(v) for k, v in (cfg.get("env") or {}).items()}}
            server = sdk["StdioServerParameters"](command=cfg["command"],
                                                  args=list(cfg.get("args") or []),
                                                  env=env)
        else:
            headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()} or None
            http_client = sdk["create_mcp_http_client"](headers=headers) \
                if headers else None
            server = sdk["streamable_http_client"](
                cfg["url"], http_client=http_client) \
                if http_client else sdk["streamable_http_client"](cfg["url"])
        return sdk["Client"](server, message_handler=self._make_handler(rt),
                             read_timeout_seconds=float(
                                 cfg.get("timeout") or DEFAULT_CALL_TIMEOUT_S))

    def _make_handler(self, rt: _Runtime):
        async def handler(message):
            # notifications/tools/list_changed -> refetch (best effort; the
            # docs promise it "if supported", so failures are swallowed)
            if getattr(message, "method", "") != "notifications/tools/list_changed":
                return
            try:
                async with rt.lock:
                    if rt.client is not None:
                        await self._refresh_tools(rt)
            except Exception:
                pass
        return handler

    async def _refresh_tools(self, rt: _Runtime) -> None:
        res = await rt.client.list_tools()
        tools, used = [], set()
        for t in getattr(res, "tools", None) or []:
            name = str(getattr(t, "name", "") or "")
            model_name = model_tool_name(rt.cfg["name"], name)
            suffix = 2
            while model_name in used:  # sanitized collisions stay addressable
                model_name = f"{model_name[:60]}_{suffix}"
                suffix += 1
            used.add(model_name)
            schema = getattr(t, "input_schema", None)
            tools.append({
                "name": name, "model_name": model_name,
                "description": str(getattr(t, "description", "") or ""),
                "input_schema": schema if isinstance(schema, dict)
                else {"type": "object", "properties": {}},
                "risk": risk_for_annotations(getattr(t, "annotations", None)),
            })
        rt.tools = tools

    async def _serve(self, name: str, rt: _Runtime) -> None:
        while not self._shutdown:
            stop = asyncio.Event()
            rt.stop = stop
            try:
                client = self._build_client(rt)
                async with client:
                    rt.client = client
                    await self._refresh_tools(rt)
                    rt.state, rt.error = "ready", ""
                    rt.last_ok, rt.attempt = now_iso(), 0
                    # hold: until stop, checking liveness with a ping
                    while not stop.is_set():
                        try:
                            await asyncio.wait_for(stop.wait(), WATCHDOG_S)
                            break
                        except asyncio.TimeoutError:
                            pass
                        try:
                            await asyncio.wait_for(client.send_ping(),
                                                   PING_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            raise RuntimeError("ping timed out — the MCP "
                                               "server stopped answering")
                        except Exception as e:  # ping unsupported (-32601) is fine
                            code = getattr(getattr(e, "error", None), "code", None)
                            if code != -32601:
                                raise RuntimeError(f"connection lost: {e}") from e
            except asyncio.CancelledError:
                break
            except BaseException as e:
                if self._shutdown:
                    break
                rt.state = "error"
                rt.error = f"{type(e).__name__}: {e}"[:300]
                if self.events is not None:
                    try:
                        self.events.publish("mcp_server_error", server=name,
                                            error=rt.error)
                    except Exception:
                        pass
            finally:
                rt.client = None
            if self._shutdown or stop.is_set():
                # a restart poke: reconnect right away, backoff reset
                continue
            delay = min(BACKOFF_MAX_S, BACKOFF_START_S * (2 ** rt.attempt))
            rt.attempt += 1
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
        rt.client = None

    # ── calls from the tool loop ────────────────────────────────────────
    def call(self, server: str, tool: str, arguments: dict, timeout: float):
        return self._submit(self._call(server, tool, arguments, timeout),
                            timeout + 15.0)

    async def _call(self, server: str, tool: str, arguments: dict, timeout: float):
        rt = self._servers.get(server)
        if rt is None:
            raise RuntimeError(f"no MCP server '{server}'")
        if rt.client is None:
            raise RuntimeError(f"MCP server '{server}' is not connected "
                               f"(state: {rt.state})")
        try:
            async with rt.lock:
                res = await asyncio.wait_for(rt.client.call_tool(tool, arguments),
                                             timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"MCP server '{server}' tool '{tool}' timed out "
                               f"after {int(timeout)} s")
        payload = result_payload(res)
        if getattr(res, "is_error", False):
            raise RuntimeError(str(payload.get("text") or "MCP tool reported an error"))
        rt.last_ok = now_iso()
        return payload

    # ── views ───────────────────────────────────────────────────────────
    def tool_specs(self) -> list[ToolSpec]:
        """ToolSpecs for every tool of every READY server (never-filtering
        and permission overrides are the caller's business, except the
        per-server tool_permissions carried on each spec)."""
        specs: list[ToolSpec] = []
        for name, rt in list(self._servers.items()):
            if rt.state != "ready" or not rt.cfg.get("enabled"):
                continue
            server = rt.cfg["name"]
            perms = rt.cfg.get("tool_permissions") or {}
            timeout = float(rt.cfg.get("timeout") or DEFAULT_CALL_TIMEOUT_S)
            for t in list(rt.tools):
                def run(args, s=server, tl=t["name"], to=timeout):
                    return self.call(s, tl, args, to)
                specs.append(ToolSpec(
                    name=t["model_name"], display=t["name"],
                    description=t["description"] or
                    f"MCP tool {t['name']} on server {server}",
                    parameters=t["input_schema"], source="mcp", risk=t["risk"],
                    run=run,
                    label=(lambda s=server, tl=t["name"]: lambda args:
                           f"using {s}: {tl}")(),
                    permission_override=str(perms.get(t["name"], "")),
                ))
        return specs

    def tool_info(self) -> list[dict]:
        """Rows for GET /api/tools (all ready servers; permission is the
        per-server override when set, else the caller resolves the default)."""
        rows = []
        for name, rt in list(self._servers.items()):
            if rt.state != "ready":
                continue
            for t in list(rt.tools):
                rows.append({"name": t["model_name"], "display": t["name"],
                             "source": "mcp", "risk": t["risk"], "available": True,
                             "description": t["description"],
                             "server": rt.cfg["name"],
                             "permission_override":
                                 str((rt.cfg.get("tool_permissions") or {})
                                     .get(t["name"], ""))})
        return rows

    def status(self, servers: list[dict]) -> list[dict]:
        """GET /api/mcp/status rows: runtime truth when the loop knows the
        server, config-only otherwise. `servers` is the merged settings list
        (so disabled/unknown entries show even before the loop runs)."""
        sdk_missing = not sdk_available()
        out = []
        for cfg in servers:
            rt = self._servers.get(cfg["name"])
            if sdk_missing:
                state, error = "unavailable", UNAVAILABLE_MSG
            elif rt is None:
                state = "disabled" if not cfg.get("enabled") else "starting"
                error = ""
            else:
                state, error = rt.state, rt.error
            out.append({"name": cfg["name"], "transport": cfg.get("transport", "stdio"),
                        "state": state, "error": error,
                        "tools": len(rt.tools) if rt else 0,
                        "last_ok": rt.last_ok if rt else ""})
        return out

    def restart(self, name: str) -> str:
        """Poke a server to reconnect now. Returns ok | unknown | disabled."""
        rt = self._servers.get(name)
        if rt is None:
            return "unknown"
        if not rt.cfg.get("enabled"):
            return "disabled"
        if (self._ensure_loop() and rt.task is not None and not rt.task.done()
                and rt.stop is not None):
            self._submit(self._poke(rt))
            return "ok"
        if self._ensure_loop():
            # task dead or still connecting: make sure it reconnects
            self._submit(self._respawn(name, rt))
            return "ok"
        return "unknown"

    async def _poke(self, rt: _Runtime):
        if rt.stop is not None:
            rt.stop.set()

    async def _respawn(self, name, rt):
        if self._servers.get(name) is rt:
            if rt.task is None or rt.task.done():
                rt.state = "starting"
                rt.task = asyncio.create_task(self._serve(name, rt))

    # ── shutdown ────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        loop = self._loop
        if loop is not None:
            async def _quit():
                for name in list(self._servers):
                    await self._teardown(name)
            try:
                asyncio.run_coroutine_threadsafe(_quit(), loop).result(10)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
