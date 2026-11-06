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
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlparse

from riko import hub_client

DEFAULT_BROWSER_PORT = 8766

# Identity the extension probes for (GET /hello) before it spends a tab on
# the link; keep in sync with pyproject.toml's version when the app ships one.
CLIENT_NAME = "riko-desktop"
CLIENT_VERSION = "1.0.0"

# Friendly status page for the extension's WebSocket tab (served at /link).
# The real work is done by the extension's link_relay.js content script; this
# page only shows whether Riko is online and reassures the owner that the
# relay reconnects by itself. When the desktop is DOWN we can't serve this at
# all — that case is handled extension-side (no tab opens until /hello
# answers), so this page never becomes a raw browser error page by design.
LINK_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Riko link</title>
<style>
 body { font: 13.5px/1.6 system-ui, sans-serif; background: #14101c;
        color: #cfc6e4; display: flex; align-items: center;
        justify-content: center; height: 100vh; margin: 0; }
 main { text-align: center; max-width: 34rem; }
 h1 { font-size: 15px; font-weight: 600; }
 #s { color: #8f83ad; font-size: 12.5px; }
</style>
</head>
<body>
<main>
  <h1>Riko link</h1>
  <p>This tab keeps Riko's browser helper connected. Keep it open.
     Closing it is fine — it reopens by itself after a minute.</p>
  <div id="s">Riko is connecting…</div>
  <p id="rc">If Riko goes offline this tab waits and reconnects
     automatically; you don't have to do anything.</p>
</main>
<script>
 addEventListener("message", (e) => {
   const d = e.data;
   if (d && d.source === "riko-link")
     document.getElementById("s").textContent =
       {up: "Riko is online", down: "Riko is offline — reconnecting…",
        reject: "token rejected (fix it in the extension options)"}[d.state] || d.state;
 });
</script>
</body>
</html>
"""
DEFAULT_APPROVAL_TIMEOUT = 120.0   # the hub's own gate matches this (API.md)
RELAY_TIMEOUT = 60.0               # one browser command round-trip
POLL_WAIT = 25.0                   # long-poll cadence, seconds

TOOL_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CLIENT_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ── Body capabilities (docs/BODY.md §1) ──────────────────────
# The desktop's built-in gesture vocabulary. The avatar page reports its real
# library (built-ins + .vrma drop-ins) over POST /control/body-capabilities
# and we re-register with that; until it does, we register this default list.
# `talk_*` stay out: they are internal sprinkles, not things you can ask for.
DEFAULT_BODY_ACTIONS = ["wave", "think", "nod", "shake_head", "look_around",
                        "stretch", "fidget", "hair_touch", "laugh", "pout",
                        "listen"]
DEFAULT_BODY_EXPRESSIONS = ["happy", "angry", "sad", "relaxed", "surprised"]

BODY_NAME_OK = re.compile(r"^[a-z0-9_-]{1,64}$")  # same grammar as the hub's


def _clean_body(raw):
    """Normalise a reported body object into {actions, expressions} of bare
    lowercase names (docs/BODY.md §1); anything unusable is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in ("actions", "expressions"):
        names = []
        for item in (raw.get(key) or []):
            # same normalisation as the hub's / the director's: spaces and
            # dashes collapse to underscores ('Shake-Head' -> 'shake_head')
            name = re.sub(r"[\s-]+", "_", str(item or "").strip().lower()).strip("_")
            if not BODY_NAME_OK.match(name) or name in names:
                continue
            names.append(name)
        out[key] = names
    return out


_current_body = {"actions": list(DEFAULT_BODY_ACTIONS),
                 "expressions": list(DEFAULT_BODY_EXPRESSIONS)}


def note_body(body):
    """Remember what this desktop can physically do right now."""
    global _current_body
    _current_body = {"actions": list(body.get("actions") or []),
                     "expressions": list(body.get("expressions") or [])}


def current_body_actions():
    """Gesture names this desktop can play right now (for greeting actions)."""
    return list(_current_body["actions"])


def _reset_body():
    note_body({"actions": DEFAULT_BODY_ACTIONS,
               "expressions": DEFAULT_BODY_EXPRESSIONS})


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


AWARENESS_HOW_TO_ENABLE = (
    "Tick 'Let her see my desktop' in Riko's tray menu, or ask the owner "
    "and call set_desktop_awareness(enabled=true) — they confirm it through "
    "the approval bubble.")


def awareness_gate_tools(sense):
    """The two tools that exist even while the privacy toggle is off, so she
    never tells the owner desktops/workspaces are 'out of my reach'
    (docs/DESKTOP_SENSE.md): a read-only status probe and the toggle itself
    (risk `act` -> the hub asks the owner, the same privacy gate as the tray).
    Needs a DesktopSense; without one there is nothing to report or flip."""
    if sense is None:
        return []
    obj = {"type": "object", "additionalProperties": False}
    on = bool(sense.awareness.enabled)
    status_desc = (
        "Check Riko's desktop awareness (the privacy toggle behind the tray "
        "item 'Let her see my desktop'). "
        + ("It is ON: the desktop_* tools (open windows, screens, open apps, "
           "workspaces) are registered right now."
           if on else
           "The desktop tools (windows, screens, open apps, workspaces) "
           "exist on this PC but are switched OFF, so she sees nothing yet. "
           "The owner can tick 'Let her see my desktop' in Riko's tray menu, "
           "or ask her to call set_desktop_awareness(enabled=true) and "
           "approve it. Returns {enabled, how_to_enable}.")
    )
    return [
        {"name": "desktop_awareness_status", "risk": "read",
         "description": status_desc,
         "parameters": dict(obj, properties={})},
        {"name": "set_desktop_awareness", "risk": "act",
         "description": "Turn desktop awareness on or off for this PC — the "
                        "owner's privacy master toggle, the same one as the "
                        "tray item 'Let her see my desktop'. On: you gain the "
                        "desktop_* tools (open windows, screens, browser "
                        "tabs, open apps, focus windows, switch "
                        "workspaces/virtual desktops — no keystrokes needed). "
                        "Off: they vanish. The owner is shown an approval "
                        "prompt first; if they deny, nothing changes.",
         "parameters": dict(obj, properties={
             "enabled": {"type": "boolean",
                         "description": "true = she may see the desktop, "
                                        "false = privacy off"}},
             required=["enabled"])},
    ]


def desktop_tools(sense=None):
    """Everything the desktop app offers the model (browser tools included —
    they answer 'the Firefox helper is not connected' until it is). With a
    DesktopSense whose awareness toggle is on, the desktop_* tools join the
    catalogue (docs/DESKTOP_SENSE.md: absent entirely while awareness is
    off — the privacy toggle is the registration gate). The two
    awareness-gate tools (status + the toggle) are always offered when a
    DesktopSense exists, so she can report the state and turn it on by
    request instead of claiming the desktop is out of reach."""
    obj = {"type": "object", "additionalProperties": False}
    tools = [
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
        *awareness_gate_tools(sense),
    ]
    if sense is not None and sense.awareness.enabled:
        from riko.desktop_sense.sense import sense_tools
        tools += sense_tools()
    return tools


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

    The same port also answers GET /riko-pair with the token over plain HTTP,
    so the extension can auto-pair without the owner pasting anything (only
    localhost can reach the socket; the token still keeps *other* local
    processes from posing as the extension after the handshake). GET /hello
    (no auth, CORS *) is the extension's reachability probe: it opens the
    /link tab only after this answers with our identity, and GET /link hosts
    the tab itself (see LINK_PAGE).
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

    async def _process_request(self, connection, request):
        path = (request.path or "").split("?")[0]
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        if path == "/riko-pair":
            return Response(200, "OK", Headers([
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Access-Control-Allow-Origin", "*"),
                # we hang up right after this body; without this header Firefox
                # keeps the socket in its keep-alive pool and then rides the
                # dead socket for the WebSocket upgrade — handshake vanishes.
                ("Connection", "close"),
            ]), self.token.encode())
        if path == "/hello":
            # Reachability probe for the extension (no auth, cross-origin):
            # background.js only spends a tab on /link once this answers with
            # our identity. CORS * so the moz-extension background page can
            # read it; Connection close for the same dead-socket reason as
            # /riko-pair above.
            return Response(200, "OK", Headers([
                ("Content-Type", "application/json"),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", "no-store"),
                ("Connection", "close"),
            ]), json.dumps({"ok": True, "name": CLIENT_NAME,
                            "version": CLIENT_VERSION}).encode())
        if path == "/link":
            # The Riko Helper extension keeps its WebSocket inside a tab on
            # this page: an http:// origin (unlike the extension's own secure
            # context, where Firefox 157 force-upgrades ws:// to wss:// and
            # never falls back). link_relay.js (content script) does the work;
            # this page is a quiet host with a friendly status line.
            return Response(200, "OK", Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", "no-store"),
                ("Connection", "close"),
            ]), LINK_PAGE.encode())
        return None  # otherwise: ordinary WebSocket handshake

    async def _serve(self):
        from websockets.asyncio.server import serve
        try:
            async with serve(self._handle, self.host, self.port,
                             max_size=32 * 1024 * 1024,
                             process_request=self._process_request):
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
        log = logging.getLogger(__name__)
        log.debug("browser bridge: socket open origin=%r", ws.request.headers.get("Origin"))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            hello = json.loads(raw)
        except Exception as e:
            log.debug("browser bridge: bad hello: %r", e)
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
                 heartbeat_interval=None, poll_wait=POLL_WAIT, body=None):
        self.hub = hub
        self.client_id = str(client_id or "").strip()
        if not CLIENT_ID_OK.match(self.client_id):
            raise ValueError(f"client_id {self.client_id!r} must match ^[A-Za-z0-9_-]{{1,64}}$")
        self.name = name or "Riko desktop"
        self.kind = kind
        self.platform_name = platform_name or sys.platform
        self.tools = [t for t in tools if TOOL_NAME_OK.match(t.get("name", ""))]
        # Body capabilities (docs/BODY.md §1): until the avatar page reports
        # its real library, register the built-in gesture list.
        self.body = _clean_body(body) if body is not None else {
            "actions": list(DEFAULT_BODY_ACTIONS),
            "expressions": list(DEFAULT_BODY_EXPRESSIONS)}
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
        # desktop.py installs these (riko-ctl approve / deny entry points)
        self.approve_hook = lambda: {"ok": False, "error": "no approval UI"}
        self.deny_hook = lambda: {"ok": False, "error": "no approval UI"}
        # awareness wiring (start_desktop / desktop.py install these)
        self.awareness_toggle = None   # fn(enabled) -> bool
        self.awareness_tray = None     # fn(enabled), Qt-queued tray tick

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
            platform=self.platform_name, tools=self.tools, body=self.body)
        if self._heartbeat_override is None:
            hb = out.get("heartbeat_s") or out.get("heartbeat_seconds") or 30
            self._heartbeat_s = min(30.0, max(5.0, float(hb)))
        self._registered.set()
        note_body(self.body)
        print(f"[client] registered on the hub as {self.client_id} "
              f"({len(self.tools)} tools, body: {len(self.body['actions'])} actions)")
        return out

    def set_tools(self, tools, executors):
        """Swap the offered catalogue at runtime (the desktop.awareness tray
        toggle adds/removes the desktop_* tools; docs/DESKTOP_SENSE.md). A
        hub that is down gets the new catalogue at the next re-register."""
        self.tools = [t for t in tools if TOOL_NAME_OK.match(t.get("name", ""))]
        self.executors = dict(executors)
        if self._registered.is_set():
            try:
                self._register()
            except hub_client.HubError as e:
                print(f"[client] the tool list changed but the hub refused a "
                      f"re-register: {e}")
                return {"ok": False, "error": str(e)}
        return {"ok": True, "tools": len(self.tools)}

    def set_body(self, raw):
        """The avatar page reports the gesture library it can actually play
        (built-ins + .vrma drop-ins; docs/BODY.md §1/§5). Re-register with the
        hub when it changed; a hub that is down just gets it next re-register.
        """
        new = _clean_body(raw)
        if new == self.body:
            return {"ok": True, "changed": False, "actions": list(self.body["actions"])}
        self.body = new
        note_body(new)
        print(f"[client] body updated: {', '.join(new['actions']) or '(no actions)'}")
        # re-register right away (idempotent; also covers the tiny window where
        # the first register landed on the hub but our thread has not resumed)
        try:
            self._register()
        except hub_client.HubError as e:
            print(f"[client] the body changed but the hub refused a re-register: {e}")
            return {"ok": False, "changed": True, "error": str(e),
                    "actions": list(new["actions"])}
        return {"ok": True, "changed": True, "actions": list(new["actions"])}

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


def make_executors(bridge, sense=None, awareness_toggle=None):
    """The name -> callable map a ClientRuntime runs (browser_* relay to
    `bridge`). `sense` (a DesktopSense) owns browser_tabs — listTabs with
    old-extension fallback — and adds the desktop_* executors when the
    awareness toggle is on. `awareness_toggle(enabled) -> bool` is the
    desktop app's single toggle path (flip + persist + live re-register +
    tray tick); without it the set tool only flips the setting, which is
    still correct for a headless runtime — the hub gets the new catalogue
    at the next re-register."""

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

    def _tabs(**kw):
        if sense is not None:
            return sense.browser_tabs()
        return relay("tabs", {})

    def _awareness_status(**_kw):
        if sense is None:
            raise ToolError("desktop awareness is unavailable on this PC")
        on = bool(sense.awareness.enabled)
        return {"enabled": on,
                "how_to_enable": AWARENESS_HOW_TO_ENABLE if not on else
                "already on: the desktop_* tools are registered"}

    def _awareness_set(enabled=True, **_kw):
        if sense is None:
            raise ToolError("desktop awareness is unavailable on this PC")
        from riko.desktop_sense.awareness import _as_bool
        want = _as_bool(enabled, None)
        if want is None:
            raise ToolError("set_desktop_awareness: enabled must be true or "
                            f"false, got {enabled!r}")
        if awareness_toggle is not None:
            new = bool(awareness_toggle(want))
        else:
            new = bool(sense.awareness.set(want))
        return {"enabled": new,
                "note": ("desktop_* tools registered (windows, screens, "
                         "apps, workspaces)" if new else
                         "desktop_* tools removed; the owner can turn them "
                         "back on via the tray or this tool")}

    ex = {
        "open_url": open_url,
        "clipboard_read": clipboard_read,
        "clipboard_write": clipboard_write,
        "notify": notify,
        "browser_tabs": _tabs,
        "browser_open": _mk("open", ("url", "new_tab")),
        "browser_read": _mk("read", ("tab",)),
        "browser_click": _mk("click", ("n",)),
        "browser_type": _mk("type", ("n", "text", "submit")),
        "browser_select": _mk("select", ("n", "value")),
        "browser_scroll": _mk("scroll", ("direction", "amount")),
        "browser_back": _mk("back", ("tab",)),
        "browser_screenshot": _mk("screenshot", ("tab",)),
        "browser_close": _mk("close", ("tab",)),
        "desktop_awareness_status": _awareness_status,
        "set_desktop_awareness": _awareness_set,
    }
    if sense is not None and sense.awareness.enabled:
        ex.update(sense.executors())
    return ex


# ── Desktop glue: what desktop.py calls ──────────────────────
def build_sense(cfg, bridge):
    """The DesktopSense for this PC (docs/DESKTOP_SENSE.md). Cheap: the
    backend is only detected when a tool actually runs."""
    try:
        from riko.desktop_sense.sense import DesktopSense
    except ImportError:
        return None
    try:
        return DesktopSense(cfg, bridge)
    except Exception as e:
        print(f"[client] desktop awareness unavailable ({e})")
        return None


def apply_awareness(runtime, cfg, bridge, sense):
    """(Re)build the offered tool catalogue from the awareness toggle; the
    tray checkbox calls this after flipping. Returns whether sense is on.
    Keeps the set-tool executor wired to the runtime's full toggle path
    (flip + persist + re-register + tray tick) when one exists."""
    on = bool(sense is not None and sense.awareness.enabled)
    toggle = getattr(runtime, "awareness_toggle", None)
    runtime.set_tools(desktop_tools(sense),
                      make_executors(bridge, sense, toggle))
    return on


def start_desktop(cfg, hub, viewer_module=None):
    """Start the browser bridge + client runtime for the desktop app.

    Returns (runtime, bridge, sense). All best-effort: a hub that is down
    just retries quietly, and a busy WS port only loses the browser tools.
    """
    d = (cfg or {}).get("desktop") or {}
    cc = d.get("client") or {}
    if not cc.get("enabled", True):
        return None, None, None
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

    sense = build_sense(cfg, bridge)

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

    # The single awareness-toggle path: the set_desktop_awareness tool, the
    # tray checkbox and `riko-ctl desktop-sense` all land here, so any of
    # them flips the setting, re-registers the catalogue with the hub RIGHT
    # AWAY (no restart), and keeps the tray tick honest (via
    # runtime.awareness_tray, which desktop.py wires to a Qt-queued signal).
    _refs = {}

    def toggle_awareness(enabled):
        new = bool(sense.awareness.set(bool(enabled)))
        rt = _refs.get("rt")
        if rt is not None:
            rt.set_tools(desktop_tools(sense),
                         make_executors(bridge, sense, toggle_awareness))
            tray_cb = getattr(rt, "awareness_tray", None)
            if tray_cb:
                try:
                    tray_cb(new)
                except Exception as e:
                    print(f"[client] tray awareness tick failed: {e}")
        print(f"[client] desktop awareness {'on' if new else 'off'} "
              f"(tools re-registered)", flush=True)
        return new

    runtime = ClientRuntime(
        hub, desktop_tools(sense), make_executors(bridge, sense, toggle_awareness),
        client_id=str(cc.get("id") or "").strip() or client_id(),
        name=str(cc.get("name") or "Riko desktop"),
        on_approval=lambda summary, respond, timeout: show_approval(
            summary, lambda ok: finish_approval(respond, ok), timeout),
        on_event=on_event,
        approval_timeout=float(cc.get("approval_timeout") or DEFAULT_APPROVAL_TIMEOUT))
    _refs["rt"] = runtime
    runtime.awareness_toggle = toggle_awareness if sense is not None else None
    runtime.awareness_tray = None  # desktop.py installs the Qt-queued tick

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
    return runtime, bridge, sense
