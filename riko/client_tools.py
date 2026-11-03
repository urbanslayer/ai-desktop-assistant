"""Client tools: the hub asks THIS PC to do things, and we do them (or ask first).

Round 2 (docs/ROUNDS.md R2-F, docs/TOOLS.md A/B1). Three moving parts live here:

1. The tool catalogue + executors: `open_url` (xdg-open, http(s) only),
   `clipboard_read` / `clipboard_write`, `notify`, and the `browser_*` tools,
   which are relayed over a local WebSocket to the "Riko Helper" Firefox
   extension (browser-ext/).
2. `ClientRuntime` — speaks the client-tool channel from docs/API.md:
   register -> long-poll requests -> execute (asking when told to) -> post
   results, plus a heartbeat loop and an /api/events long-poll used only to
   show tool progress in the UI.
3. `BrowserBridge` — the token-authenticated WebSocket server on
   127.0.0.1:<port> the extension connects to. The per-install token lives
   in ~/.config/riko/browser_token (0600) and is shown by `riko-ctl browser-token`.

Approvals: a request with `needs_approval` (or `tool: "__approval__"`, which is
an approval for a *hub* tool) goes through `on_approval(summary, respond)` —
desktop.py shows the tray/bubble prompt; a timeout or no UI means deny. The
extension can also refuse to run a click/type it considers `danger` (send/buy/
pay/... — see browser-ext/lib/risk.js) by answering `needs_confirmation`; the
runtime then asks the owner and re-sends with `confirmed: true`. That upgrade
fires whatever the hub-side permission says: danger always asks.

Nothing in here needs Qt; desktop.py passes the UI callbacks in.
"""
import asyncio
import concurrent.futures
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlparse

from riko import hub_client

DEFAULT_BROWSER_PORT = 8765
DEFAULT_APPROVAL_TIMEOUT = 120.0   # the hub's own gate matches this (API.md)
RELAY_TIMEOUT = 60.0               # one browser command round-trip
POLL_WAIT = 25.0                   # long-poll cadence, seconds

TOOL_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CLIENT_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ToolError(Exception):
    """The tool ran but failed: reported to the hub as {ok: false, error}."""


class NeedConfirmation(Exception):
    """The extension refused to act without a human OK (danger upgrade).
    `reason` becomes the approval question."""

    def __init__(self, reason, method, params):
        super().__init__(reason)
        self.reason = reason
        self.method = method
        self.params = params


class BrowserNotConnected(Exception):
    """The Firefox helper isn't on the WebSocket right now."""


# ── Per-install identity: client id + extension token ────────
def config_dir():
    import pathlib
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(os.environ.get("RIKO_CONFIG_DIR")
                        or pathlib.Path(base) / "riko")


def _read_or_make(path, make, mode=0o600):
    """Return the stored value, creating it once with tight perms."""
    import pathlib
    path = pathlib.Path(path)
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = make()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return value


def client_id():
    """Stable id on the hub's client channel, e.g. `my-desktop`."""
    def make():
        user = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("USER")
                      or os.environ.get("USERNAME") or "someone") or "someone"
        return f"{user}-desktop"[:64]
    return _read_or_make(config_dir() / "client_id", make, 0o644)


def browser_token():
    """The per-install token the Firefox extension must present."""
    return _read_or_make(config_dir() / "browser_token",
                         lambda: secrets.token_urlsafe(24))


# ── The tool catalogue (registered on the hub as desktop__<name>) ──
_OPENERS = ("xdg-open", "gio")


def _open_command(url):
    if sys.platform == "win32":
        return ["cmd", "/c", "start", "", url]
    for name in _OPENERS:
        exe = shutil.which(name)
        if exe:
            return [exe] + ([url] if name == "xdg-open" else ["open", url])
    raise ToolError("no xdg-open or gio on this PC, can't open URLs")


def open_url(url):
    """Open an http(s) link in the owner's default browser (risk: read)."""
    url = str(url or "").strip()
    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise ToolError(f"only http(s) URLs are allowed, got {url!r}")
    try:
        done = subprocess.run(_open_command(url), capture_output=True,
                              timeout=15, text=True)
    except subprocess.TimeoutExpired:
        raise ToolError("the open command never returned") from None
    if done.returncode != 0:
        raise ToolError(f"open failed: {(done.stderr or done.stdout or '').strip()[:200]}")
    return f"opened {url}"


def _run_first(candidates, timeout=10, input_text=None):
    """Run the first candidate tool that exists; ToolError if none does."""
    for argv in candidates:
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            done = subprocess.run([exe, *argv[1:]], capture_output=True,
                                  timeout=timeout, text=True, input=input_text)
        except subprocess.TimeoutExpired:
            raise ToolError(f"{argv[0]} timed out") from None
        if done.returncode != 0:
            raise ToolError(f"{argv[0]} failed: {(done.stderr or '').strip()[:200]}")
        return done.stdout
    raise ToolError("no clipboard tool found (install wl-clipboard or xclip)")


def clipboard_read():
    """Text on the clipboard (risk: act — the hub asks by default)."""
    for argv in (["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
                 ["xsel", "--clipboard", "--output"]):
        if shutil.which(argv[0]):
            return _run_first([argv])
    raise ToolError("no clipboard tool found (install wl-clipboard or xclip)")


def clipboard_write(text):
    text = "" if text is None else str(text)
    for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]):
        if shutil.which(argv[0]):
            _run_first([argv], input_text=text)
            return f"copied {len(text)} chars"
    raise ToolError("no clipboard tool found (install wl-clipboard or xclip)")


_notify_fallback = None  # desktop.py installs a tray-message fallback here


def set_notify_fallback(fn):
    """fn(title, text) used when notify-send isn't around (Qt tray)."""
    global _notify_fallback
    _notify_fallback = fn


def notify(text, title="Riko"):
    text = str(text or "").strip()
    if not text:
        raise ToolError("notify: nothing to say")
    exe = shutil.which("notify-send")
    if exe:
        done = subprocess.run([exe, str(title or "Riko"), text],
                              capture_output=True, timeout=15, text=True)
        if done.returncode != 0:
            raise ToolError(f"notify-send failed: {(done.stderr or '').strip()[:200]}")
        return "notified"
    if _notify_fallback:
        _notify_fallback(str(title or "Riko"), text)
        return "notified (tray)"
    print(f"[notify] {title}: {text}")
    return "printed (no notifier available)"


def browser_tools():
    """The browser_* catalogue relayed to the Firefox helper (docs/TOOLS.md B1).
    Risks per docs/ROUNDS.md R2-F; click/type can self-upgrade to danger inside
    the extension, which the runtime honours whatever the hub setting says."""
    obj = {"type": "object", "additionalProperties": False}
    return [
        {"name": "browser_tabs", "risk": "read",
         "description": "List the Firefox tabs Riko can see (numbered).",
         "parameters": dict(obj, properties={})},
        {"name": "browser_open", "risk": "read",
         "description": "Open a URL in the owner's Firefox (new tab by default).",
         "parameters": dict(obj, properties={
             "url": {"type": "string", "description": "http(s) URL"},
             "new_tab": {"type": "boolean", "description": "false = replace the active tab"}},
             required=["url"])},
        {"name": "browser_read", "risk": "read",
         "description": "Read a tab: title, URL, readable text and a numbered list of "
                        "links/buttons/inputs to click/type/select by number. Password values are never shown.",
         "parameters": dict(obj, properties={
             "tab": {"type": "integer", "description": "tab number from browser_tabs; default is the active tab"}})},
        {"name": "browser_click", "risk": "act",
         "description": "Click numbered element n from the last browser_read.",
         "parameters": dict(obj, properties={"n": {"type": "integer"}}, required=["n"])},
        {"name": "browser_type", "risk": "act",
         "description": "Type text into numbered element n (never a password field). "
                        "submit=true also presses Enter (that always asks).",
         "parameters": dict(obj, properties={
             "n": {"type": "integer"}, "text": {"type": "string"},
             "submit": {"type": "boolean", "description": "press Enter after typing"}},
             required=["n", "text"])},
        {"name": "browser_select", "risk": "act",
         "description": "Pick option value in numbered <select> n.",
         "parameters": dict(obj, properties={"n": {"type": "integer"},
                                             "value": {"type": "string"}},
                            required=["n", "value"])},
        {"name": "browser_scroll", "risk": "read",
         "description": "Scroll the page: direction up|down|top|bottom.",
         "parameters": dict(obj, properties={
             "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]},
             "amount": {"type": "integer", "description": "pixels for up/down (default one screen)"}})},
        {"name": "browser_back", "risk": "read",
         "description": "Go back in a tab's history.",
         "parameters": dict(obj, properties={
             "tab": {"type": "integer", "description": "default is the active tab"}})},
        {"name": "browser_screenshot", "risk": "read",
         "description": "Screenshot the visible part of a tab (data: URL, base64 PNG).",
         "parameters": dict(obj, properties={
             "tab": {"type": "integer", "description": "default is the active tab"}})},
        {"name": "browser_close", "risk": "act",
         "description": "Close a tab (number from browser_tabs; default is the active tab).",
         "parameters": dict(obj, properties={
             "tab": {"type": "integer", "description": "default is the active tab"}})},
    ]


def desktop_tools():
    """Everything the desktop app offers the model (browser tools included —
    they answer 'the Firefox helper is not connected' until it is)."""
    obj = {"type": "object", "additionalProperties": False}
    return [
        {"name": "open_url", "risk": "read",
         "description": "Open an http(s) URL in the user's default browser.",
         "parameters": dict(obj, properties={"url": {"type": "string"}}, required=["url"])},
        {"name": "clipboard_read", "risk": "act",
         "description": "Read the text on the user's clipboard.",
         "parameters": dict(obj, properties={})},
        {"name": "clipboard_write", "risk": "act",
         "description": "Put text on the user's clipboard.",
         "parameters": dict(obj, properties={"text": {"type": "string"}}, required=["text"])},
        {"name": "notify", "risk": "read",
         "description": "Show a desktop notification to the user.",
         "parameters": dict(obj, properties={"text": {"type": "string"},
                                             "title": {"type": "string"}},
                            required=["text"])},
        *browser_tools(),
    ]


# ── The Firefox side: token-authenticated WebSocket server ───
class BrowserBridge:
    """Serves the Riko Helper extension on ws://127.0.0.1:<port>.

    Protocol (JSON text frames):
      ext -> us: {"type":"hello","token":...,"name":"Riko Helper",...}
      us  -> ext: {"type":"hello_ok"}  or hello_reject + close (bad token)
      us  -> ext: {"id":"...","method":"read","params":{...}}
      ext -> us: {"id":"...","ok":true,"result":...} / {"ok":false,"error":...}
                / {"ok":false,"needs_confirmation":true,"reason":"..."}
      ext -> us: {"type":"event", ...}  (fire and forget; currently ignored)
    One extension at a time; a newer hello replaces the older connection.
    """

    def __init__(self, token, host="127.0.0.1", port=DEFAULT_BROWSER_PORT):
        self.token = token
        self.host = host
        self.port = port
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._start_error = None
        self._conn = None
        self._conn_name = ""
        self._pending = {}

    # lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="riko-browser-ws")
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError(f"the browser WebSocket server never came up "
                               f"on {self.host}:{self.port}")
        if self._start_error:
            raise RuntimeError(f"can't listen on {self.host}:{self.port}: {self._start_error}")

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # bind failure etc — surface it in start()
            self._start_error = str(e)
            self._ready.set()

    async def _serve(self):
        from websockets.asyncio.server import serve
        try:
            async with serve(self._handle, self.host, self.port,
                             max_size=32 * 1024 * 1024):
                self._ready.set()
                await asyncio.get_running_loop().create_future()  # forever
        except OSError as e:
            self._start_error = str(e)
            self._ready.set()

    def stop(self):
        loop = self._loop
        if loop and loop.is_running():
            async def bye():
                if self._conn is not None:
                    await self._conn.close()
            asyncio.run_coroutine_threadsafe(bye(), loop)

    # state
    def connected(self):
        return self._conn is not None

    def extension_name(self):
        return self._conn_name

    def _fail_pending(self, why):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(BrowserNotConnected(why))
        self._pending.clear()

    # handler
    async def _handle(self, ws):
        from websockets.exceptions import ConnectionClosed
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            hello = json.loads(raw)
        except Exception:
            await ws.close(code=4400, reason="say hello first")
            return
        if not isinstance(hello, dict) or hello.get("token") != self.token:
            try:
                await ws.send(json.dumps({"type": "hello_reject",
                                          "error": "bad token"}))
            except Exception:
                pass
            await ws.close(code=4401, reason="bad token")
            return
        old = self._conn
        self._conn = ws
        self._conn_name = str(hello.get("name") or "Riko Helper")[:100]
        await ws.send(json.dumps({"type": "hello_ok"}))
        if old is not None and old is not ws:
            try:
                await old.close(code=4000, reason="replaced by a newer connection")
            except Exception:
                pass
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                fut = self._pending.get(msg.get("id"))
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except ConnectionClosed:
            pass
        finally:
            if self._conn is ws:
                self._conn = None
                self._conn_name = ""
                self._fail_pending("the Firefox helper disconnected")

    # calling
    def call(self, method, params=None, timeout=RELAY_TIMEOUT):
        """Send one command to the extension and wait for its answer.
        Raises BrowserNotConnected when no extension is on the wire."""
        loop = self._loop
        if loop is None or self._conn is None:
            raise BrowserNotConnected("the Riko Helper Firefox extension is not "
                                      "connected (is Firefox running?)")
        req_id = secrets.token_hex(8)

        async def go():
            fut = loop.create_future()
            self._pending[req_id] = fut
            try:
                await self._conn.send(json.dumps({"id": req_id, "method": method,
                                                  "params": params or {}}))
                return await asyncio.wait_for(fut, timeout)
            finally:
                self._pending.pop(req_id, None)

        try:
            return asyncio.run_coroutine_threadsafe(go(), loop).result(timeout + 2)
        except concurrent.futures.TimeoutError:
            raise BrowserNotConnected(f"the Firefox helper did not answer '{method}' "
                                      f"within {int(timeout)} s") from None


# ── The hub side: register / heartbeat / requests / results ──
class ClientRuntime:
    """Runs the client-tool channel for one hub connection (docs/API.md).

    on_approval(summary, respond, timeout) must show a yes/no prompt and call
    respond(True/False) later; None means no UI -> everything is denied.
    on_event(kind, event) receives hub events (tool_started/…) for progress UI.
    """

    def __init__(self, hub, tools, executors, client_id=None, name=None,
                 kind="desktop", platform_name=None, on_approval=None,
                 on_event=None, approval_timeout=DEFAULT_APPROVAL_TIMEOUT,
                 heartbeat_interval=None, poll_wait=POLL_WAIT):
        self.hub = hub
        self.client_id = str(client_id or "").strip()
        if not CLIENT_ID_OK.match(self.client_id):
            raise ValueError(f"client_id {self.client_id!r} must match ^[A-Za-z0-9_-]{{1,64}}$")
        self.name = name or "Riko desktop"
        self.kind = kind
        self.platform_name = platform_name or sys.platform
        self.tools = [t for t in tools if TOOL_NAME_OK.match(t.get("name", ""))]
        self.executors = dict(executors)
        self.on_approval = on_approval
        self.on_event = on_event
        self.approval_timeout = float(approval_timeout)
        self.poll_wait = float(poll_wait)
        self._heartbeat_override = float(heartbeat_interval) if heartbeat_interval else None
        self._heartbeat_s = float(heartbeat_interval or 30.0)
        self._registered = threading.Event()
        self._approval_lock = threading.Lock()
        self._approval_pending = None  # (summary, respond)
        self._threads = []
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="riko-tool")
        self._stop = threading.Event()

    # ── lifecycle ────────────────────────────────────────────
    def start(self):
        for target, name in ((self._poll_loop, "riko-client-poll"),
                             (self._heartbeat_loop, "riko-client-beat"),
                             (self._events_loop, "riko-client-events")):
            th = threading.Thread(target=target, daemon=True, name=name)
            th.start()
            self._threads.append(th)
        return self

    def stop(self):
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.hub.client_unregister(self.client_id)
        except hub_client.HubError:
            pass

    def wait_registered(self, timeout=10):
        return self._registered.wait(timeout)

    def _register(self):
        out = self.hub.client_register(
            self.client_id, name=self.name, kind=self.kind,
            platform=self.platform_name, tools=self.tools)
        if self._heartbeat_override is None:
            hb = out.get("heartbeat_s") or out.get("heartbeat_seconds") or 30
            self._heartbeat_s = min(30.0, max(5.0, float(hb)))
        self._registered.set()
        print(f"[client] registered on the hub as {self.client_id} "
              f"({len(self.tools)} tools)")
        return out

    # ── threads ──────────────────────────────────────────────
    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                if not self._registered.is_set():
                    self._register()
                body = self.hub.client_requests(self.client_id, wait=self.poll_wait)
            except hub_client.HubError as e:
                if getattr(e, "status", None) == 404:  # hub restarted: we're gone
                    self._registered.clear()
                elif not isinstance(e, hub_client.HubUnreachable):
                    print(f"[client] {e}")
                self._stop.wait(2.0)
                continue
            for req in body.get("requests") or []:
                self._pool.submit(self._safe_run, req,
                                  float(body.get("approval_timeout_s")
                                        or self.approval_timeout))

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            self._stop.wait(self._heartbeat_s)
            if self._stop.is_set() or not self._registered.is_set():
                continue
            try:
                self.hub.client_heartbeat(self.client_id)
            except hub_client.HubError as e:
                if getattr(e, "status", None) == 404:
                    self._registered.clear()  # poll thread re-registers
                elif not isinstance(e, hub_client.HubUnreachable):
                    print(f"[client] heartbeat: {e}")

    def _events_loop(self):
        """GET /api/events purely for UI progress; safe on hubs without it."""
        cursor = 0
        while not self._stop.is_set():
            try:
                out = self.hub.events(after=cursor, wait=self.poll_wait)
            except hub_client.HubError as e:
                if getattr(e, "status", None) == 404:  # old hub, no events route
                    return
                self._stop.wait(2.0)
                continue
            cursor = int(out.get("last") or cursor)
            if not self.on_event:
                continue
            for ev in out.get("events") or []:
                try:
                    self.on_event(str(ev.get("type") or ""), ev)
                except Exception as e:
                    print(f"[client] event handler: {e}")

    # ── approvals ────────────────────────────────────────────
    def ask_approval(self, summary, timeout=None):
        """Show the prompt and wait. Timeout or no UI = deny."""
        timeout = self.approval_timeout if timeout is None else float(timeout)
        answer = {}
        done = threading.Event()

        def respond(ok):
            if "ok" not in answer:
                answer["ok"] = bool(ok)
                done.set()

        with self._approval_lock:
            self._approval_pending = (str(summary or ""), respond)
        try:
            if self.on_approval is None:
                print(f"[client] approval requested but there is no UI: deny "
                      f"({summary})")
                return False
            self.on_approval(str(summary or ""), respond, float(timeout))
            if not done.wait(timeout):
                respond(False)
        finally:
            with self._approval_lock:
                self._approval_pending = None
        return bool(answer.get("ok"))

    def resolve_approval(self, ok):
        """Answer the open prompt (bubble Enter/Esc, riko-ctl approve|deny)."""
        with self._approval_lock:
            pending = self._approval_pending
        if pending is None:
            return False
        pending[1](bool(ok))
        return True

    def pending_approval(self):
        with self._approval_lock:
            return self._approval_pending[0] if self._approval_pending else None

    # ── request execution ────────────────────────────────────
    def _safe_run(self, req, approval_timeout_s):
        try:
            self._run_request(req, approval_timeout_s)
        except hub_client.HubError as e:
            print(f"[client] could not post a result: {e}")
        except Exception as e:
            print(f"[client] {type(e).__name__}: {e}")

    def _run_request(self, req, approval_timeout_s):
        rid = str(req.get("request_id") or "")
        tool = str(req.get("tool") or "")
        args = req.get("arguments") if isinstance(req.get("arguments"), dict) else {}
        summary = str(req.get("summary") or tool)
        gate = min(float(approval_timeout_s), self.approval_timeout)

        def post(**kw):
            self.hub.client_result(self.client_id, rid, **kw)

        if tool == "__approval__":  # approval-only: show, answer, run nothing
            summary = str(args.get("summary") or summary)
            ok = self.ask_approval(summary, gate)
            post(ok=True, approved=ok,
                 result="approved" if ok else "denied",
                 error="" if ok else "the user denied this action")
            return

        if req.get("needs_approval") and not self.ask_approval(summary, gate):
            post(ok=False, approved=False, error="the user denied this action")
            return

        fn = self.executors.get(tool)
        if fn is None:
            post(ok=False, approved=True, error=f"this client has no tool '{tool}'")
            return
        try:
            result = fn(**args)
        except NeedConfirmation as c:
            # The extension upgraded this to danger — always ask, per policy.
            if not self.ask_approval(c.reason, gate):
                post(ok=False, approved=False, error="the user denied this action")
                return
            try:
                result = fn(confirmed=True, **{k: v for k, v in c.params.items()
                                               if k != "confirmed"})
            except ToolError as e2:
                post(ok=False, approved=True, error=str(e2))
                return
            post(ok=True, approved=True, result=result, error="")
            return
        except ToolError as e:
            post(ok=False, approved=True, error=str(e))
            return
        except TypeError as e:  # the model called with bad arguments
            post(ok=False, approved=True, error=f"bad arguments: {e}")
            return
        post(ok=True, approved=True, result=result, error="")


def make_executors(bridge):
    """The name -> callable map a ClientRuntime runs (browser_* relay to `bridge`)."""

    def relay(method, params):
        if bridge is None:
            raise ToolError("browser tools are off on this PC")
        try:
            answer = bridge.call(method, params)
        except BrowserNotConnected as e:
            raise ToolError(str(e)) from None
        if not isinstance(answer, dict):
            raise ToolError("the Firefox helper answered something odd")
        if answer.get("needs_confirmation"):
            raise NeedConfirmation(str(answer.get("reason") or
                                       "this looks like a risky action"),
                                   method, params)
        if not answer.get("ok"):
            raise ToolError(str(answer.get("error") or "the browser tool failed"))
        return answer.get("result")

    def _mk(method, keys):
        def run(confirmed=False, **kw):
            params = {k: kw[k] for k in keys if k in kw}
            if confirmed:
                params["confirmed"] = True
            return relay(method, params)
        return run

    return {
        "open_url": open_url,
        "clipboard_read": clipboard_read,
        "clipboard_write": clipboard_write,
        "notify": notify,
        "browser_tabs": _mk("tabs", []),
        "browser_open": _mk("open", ("url", "new_tab")),
        "browser_read": _mk("read", ("tab",)),
        "browser_click": _mk("click", ("n",)),
        "browser_type": _mk("type", ("n", "text", "submit")),
        "browser_select": _mk("select", ("n", "value")),
        "browser_scroll": _mk("scroll", ("direction", "amount")),
        "browser_back": _mk("back", ("tab",)),
        "browser_screenshot": _mk("screenshot", ("tab",)),
        "browser_close": _mk("close", ("tab",)),
    }


# ── Desktop glue: what desktop.py calls ──────────────────────
def start_desktop(cfg, hub, viewer_module=None):
    """Start the browser bridge + client runtime for the desktop app.

    Returns (runtime, bridge). Both are best-effort: a hub that is down just
    retries quietly, and a busy WS port only loses the browser tools.
    """
    d = (cfg or {}).get("desktop") or {}
    cc = d.get("client") or {}
    if not cc.get("enabled", True):
        return None, None
    if viewer_module is None:
        from riko import viewer as viewer_module
    viewer = viewer_module

    port = int(cc.get("browser_port") or DEFAULT_BROWSER_PORT)
    bridge = None
    try:
        bridge = BrowserBridge(browser_token(), "127.0.0.1", port)
        bridge.start()
        print(f"[client] Firefox helper: ws://127.0.0.1:{port} "
              f"(token: riko-ctl browser-token)")
    except Exception as e:
        print(f"[client] the browser WebSocket server didn't start ({e}); "
              f"browser tools will report 'not connected'")

    def show_approval(summary, respond, timeout):
        viewer.emit("approval", summary=summary, timeout_s=int(timeout))
        print(f"[approval] Can I {summary}?  (riko-ctl approve / deny, "
              f"Enter = yes, Esc in her window = no)")

    def on_event(kind, ev):
        if kind == "tool_started":
            viewer.emit("status", state="thinking")
            viewer.emit("tool", phase="start", tool=str(ev.get("tool") or ""),
                        label=str(ev.get("label") or ""))
        elif kind == "tool_finished":
            viewer.emit("tool", phase="end", tool=str(ev.get("tool") or ""))
        elif kind == "approval_needed":
            pass  # the actual prompt arrives as a __approval__/needs_approval request

    def finish_approval(respond, ok):
        respond(ok)
        viewer.emit("approval_done")

    runtime = ClientRuntime(
        hub, desktop_tools(), make_executors(bridge),
        client_id=str(cc.get("id") or "").strip() or client_id(),
        name=str(cc.get("name") or "Riko desktop"),
        on_approval=lambda summary, respond, timeout: show_approval(
            summary, lambda ok: finish_approval(respond, ok), timeout),
        on_event=on_event,
        approval_timeout=float(cc.get("approval_timeout") or DEFAULT_APPROVAL_TIMEOUT))

    def approve(_args=None):
        ok = runtime.resolve_approval(True)
        viewer.emit("approval_done")
        return {"ok": True, "answered": ok}

    def deny(_args=None):
        ok = runtime.resolve_approval(False)
        viewer.emit("approval_done")
        return {"ok": True, "answered": ok}

    runtime.approve_hook, runtime.deny_hook = approve, deny
    runtime.start()
    return runtime, bridge
