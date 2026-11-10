"""Shared test scaffolding: a fake Riko hub and fixtures.

Everything here talks only to 127.0.0.1 and needs no mic, GPU, or internet.
The fake hub speaks the subset of docs/DESIGN.md + docs/VOICE.md the clients
use, so these tests pass with or without the real hub/ package.
"""
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# ── Owner-state isolation (must run before any riko import) ──
# The desktop app persists runtime state next to the owner's config
# (~/.config/riko/listen_mode.json, roam_x.json, the client id + browser
# token, the avatar cache). Without this, tests read the OWNER's choices:
# an 'always' listening mode made every Session build a real AlwaysListener
# on the live mic (thousands of sounddevice callbacks, PortAudio teardown
# segfaults the whole pytest run). Point every override at one throwaway
# dir; per-test monkeypatches still win. RIKO_* uses setdefault so CI can
# pin its own values; the state dir itself is absolute, so HOME is safe.
_TEST_STATE_ROOT = tempfile.mkdtemp(prefix="riko-test-state-")
os.environ.setdefault("RIKO_APP_STATE_DIR", os.path.join(_TEST_STATE_ROOT, "appstate"))
os.environ.setdefault("RIKO_ROAM_STATE", os.path.join(_TEST_STATE_ROOT, "roam_x.json"))
os.environ.setdefault("RIKO_CONFIG_DIR", os.path.join(_TEST_STATE_ROOT, "config"))
os.environ.setdefault("RIKO_AVATAR_CACHE", os.path.join(_TEST_STATE_ROOT, "avatars"))
# Belt and braces: anything resolving XDG_CONFIG_HOME/riko also lands in
# the throwaway dir instead of the owner's ~/.config/riko.
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(_TEST_STATE_ROOT, "xdg"))

# ── Owner-key isolation: the checkout's .env must not steer tests ──
# riko/config.py runs load_dotenv(ROOT/".env") at import time, so one real
# key in the owner's .env (or exported in their shell) lands in os.environ
# the moment anything imports riko and silently changes outcomes: the
# in-process asr_server starts demanding auth (keyless TestClient requests
# turn into 401s) and the hub picks up the owner's LLM/HA defaults. Force
# that .env load to happen right now (import is cached, and load_dotenv
# never re-reads or overwrites existing vars), then drop the secrets for
# the whole run. Tests that need a key set it explicitly (monkeypatch.
# setenv / a subprocess env), so this only removes ambient noise.
import riko.config as _riko_config  # noqa: E402,F401 — triggers load_dotenv here
for _secret_env in ("ASR_API_KEY", "RIKO_HUB_KEY", "LLM_API_KEY",
                    "RIKO_LLM_API_KEY", "TTS_API_KEY", "GROQ_API_KEY",
                    "FISH_API_KEY", "RIKO_HA_URL", "RIKO_HA_TOKEN"):
    os.environ.pop(_secret_env, None)
del _secret_env

# ── No real audio devices ────────────────────────────────────
# Same reason: a real PortAudio stream in a test process races pytest's
# teardown and aborts it (exit 139/134 after 'passed'). Tests inject their
# fake (tests/test_listen_mode monkeypatches riko.asr.sd), so anything
# still reaching the sounddevice module is a bug — fail loudly, not loudly
# in a few seconds' worth of deprecation warnings.
try:
    import sounddevice as _sounddevice
except ImportError:  # headless CI without the wheel: nothing to guard
    pass
else:

    def _no_real_audio(*args, **kwargs):
        raise RuntimeError("tests must not open real audio devices: "
                           "inject a fake sd module (monkeypatch.setattr(asr, 'sd', fake))")

    for _fn in ("InputStream", "OutputStream", "play", "rec", "record"):
        setattr(_sounddevice, _fn, _no_real_audio)

# ── No compositor / session bus (owner rule after an incident) ──
# A child's "nested headless Hyprland" test crashed the owner's LIVE
# Hyprland (SIGSEGV, all their apps died with it). From here on:
#  1. the test run inherits NO session env: hyprctl/swaymsg/qdbus/gdbus
#     cannot find the live session, and subprocesses started by tests
#     inherit the cleaned environment. tests/conftest runs before any test
#     module, so detection code (riko.desktop_sense) sees "no compositor"
#     unless a test injects its own env dict explicitly;
#  2. any subprocess.run/Popen aimed at a compositor, window manager,
#     display server or session-bus tool raises immediately — the backends
#     take a `runner=` fake, so reaching this guard at all is a bug.
for _session_env in ("HYPRLAND_INSTANCE_SIGNATURE", "HYPRLAND_CMD",
                     "WAYLAND_DISPLAY", "DISPLAY", "SWAYSOCK",
                     "DBUS_SESSION_BUS_ADDRESS"):
    os.environ.pop(_session_env, None)
del _session_env

import subprocess as _subprocess  # noqa: E402

_COMPOSITOR_ARGV0 = re.compile(
    r"^(hyprland|start-hyprland|hyprctl|sway|swaymsg|swaynag|kwin[-\w]*|"
    r"kwrapper[-\w]*|startplasma[-\w]*|plasmashell|gnome-shell[-\w]*|"
    r"gnome-session[-\w]*|mutter[-\w]*|weston[-\w]*|cage|labwc|niri|river|"
    r"wayfire|waymonad|xorg[-\w]*|xwayland|xvfb|xephyr|xinit|startx|"
    r"sddm[-\w]*|gdm[-\w]*|lightdm[-\w]*|lxdm[-\w]*|i3[-\w]*|openbox[-\w]*|"
    r"xfwm4[-\w]*|xfce4-session|lxqt-session|qdbus[-\w]*|gdbus|busctl|"
    r"dbus-send|grim|slurp)$", re.I)


def _guard_compositor(name):
    raise RuntimeError(
        f"tests must not start or query a real compositor/session bus "
        f"({name!r}): owner rule after a nested-compositor crash killed "
        f"their live Hyprland. Inject a fake runner "
        f"(riko/desktop_sense/util.run(runner=...)) or a recorded fixture.")


def _guarded(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args") or kwargs.get("program")
        if isinstance(argv, (list, tuple)) and argv:
            first = str(argv[0])
        elif isinstance(argv, str):
            first = argv.strip().split(" ")[0] if argv.strip() else ""
        else:
            first = ""
        if first and _COMPOSITOR_ARGV0.match(os.path.basename(first)):
            _guard_compositor(first)
        return fn(*args, **kwargs)
    return wrapper


_subprocess.run = _guarded(_subprocess.run)
_subprocess.Popen = _guarded(_subprocess.Popen)

# ── No real keystrokes / clipboard writes (docs/TYPING.md testing rules) ──
# The typing pack takes runner= fakes for ydotool and wl-clipboard alike, so
# reaching a real ydotool/ydotoold/wl-copy/wl-paste/kate from a test is a
# bug: hard-fail it the same way as the compositor guard above.
_TYPING_ARGV0 = re.compile(
    r"^(ydotool|ydotoold|wl-copy|wl-paste|wl-run|xclip|xsel|kate[-\w]*)$", re.I)


def _guarded_typing(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args") or kwargs.get("program")
        if isinstance(argv, (list, tuple)) and argv:
            first = str(argv[0])
        elif isinstance(argv, str):
            first = argv.strip().split(" ")[0] if argv.strip() else ""
        else:
            first = ""
        if first and _TYPING_ARGV0.match(os.path.basename(first)):
            raise RuntimeError(
                f"tests must not run {first!r} for real (docs/TYPING.md "
                f"testing rules): no real keystrokes, clipboard writes or "
                f"Kate — inject a fake runner.")
        return fn(*args, **kwargs)
    return wrapper


_subprocess.run = _guarded_typing(_subprocess.run)
_subprocess.Popen = _guarded_typing(_subprocess.Popen)

# ── No real commands (docs/COMMANDS.md testing rules) ──────────
# run_command takes a runner= fake, so a test reaching the real
# subprocess path INSIDE riko/commands is a bug: hard-fail it. A test that
# genuinely wants to exec something harmless (a tmp fixture script) opts in
# with RIKO_COMMANDS_ALLOW_REAL=<dir> and must launch an executable under
# exactly that dir — nothing else (no model-chosen `df`, no ambient PATH
# program) may run for real.
_COMMANDS_ALLOW_ENV = "RIKO_COMMANDS_ALLOW_REAL"


def _caller_in_commands_pkg():
    import inspect
    start = inspect.currentframe()
    frame = start.f_back if start is not None else None
    while frame is not None:
        f = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if os.sep + "riko" + os.sep + "commands" + os.sep in f:
            return True
        frame = frame.f_back
    return False


def _guarded_commands(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _caller_in_commands_pkg():
            argv = args[0] if args else kwargs.get("args") or kwargs.get("program")
            allow = os.environ.get(_COMMANDS_ALLOW_ENV) or ""
            first = ""
            if isinstance(argv, (list, tuple)) and argv:
                first = str(argv[0])
            elif isinstance(argv, str):
                first = argv.strip().split(" ")[0]
            ok = bool(allow) and os.path.isabs(first) \
                and os.path.normpath(first).startswith(
                    os.path.normpath(allow) + os.sep)
            if not ok:
                raise RuntimeError(
                    f"tests must not run real commands ({first!r}): inject a "
                    f"fake runner (riko/commands runner=) or opt into a tmp "
                    f"fixture binary via {_COMMANDS_ALLOW_ENV}=<dir> "
                    f"(docs/COMMANDS.md testing rules).")
        return fn(*args, **kwargs)
    return wrapper


_subprocess.run = _guarded_commands(_subprocess.run)
_subprocess.Popen = _guarded_commands(_subprocess.Popen)

# ...and no Obsidian Local REST API either: any TCP connect to the plugin's
# ports (127.0.0.1:27123/27124) raises; tests use a fake HTTP server on an
# ephemeral port. The bare socket.connect guard leaks past the dial paths
# that do NOT go through it: socket.create_connection dials via its own
# code path, http.client/urllib reach the socket through HTTPConnection,
# and ssl.SSLSocket overrides connect/connect_ex for the TLS handshake —
# so every one of those entry points gets its own guard. All filters key
# on loopback host + 27123/27124, so the fake servers on ephemeral ports
# keep working.
_OBSIDIAN_PORTS = {27123, 27124}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


def _obsidian_error(port):
    return RuntimeError(
        f"tests must not contact Obsidian Local REST API "
        f"(127.0.0.1:{port}): use a fake HTTP server "
        f"(docs/TYPING.md testing rules).")


def _is_obsidian_addr(addr):
    return (isinstance(addr, tuple) and len(addr) >= 2
            and str(addr[0]) in _LOOPBACK_HOSTS
            and addr[1] in _OBSIDIAN_PORTS)


# Same story for AF_UNIX: the live ydotool user service owns
# /run/user/<uid>/.ydotool_socket (and legacy /run or /tmp sockets). Tests
# must probe fake listeners bound under tmp_path instead. The pattern is
# deliberately narrow so pytest tmp sockets keep working. The guard covers
# SOCK_DGRAM too (connect/connect_ex are the same methods for either type)
# plus sendto/send: on a datagram socket those dial an unconnected path and
# put BYTES on the daemon without ever calling connect().
_YDOTOOL_SOCK_RE = re.compile(r"^(/run/user/\d+|/run|/tmp)/\.ydotool_socket$")


def _is_ydotool_socket_path(addr):
    return isinstance(addr, str) and bool(_YDOTOOL_SOCK_RE.match(addr))


def _ydotool_socket_error(addr):
    return RuntimeError(
        f"tests must not contact the real ydotool daemon socket ({addr}): "
        f"bind a fake AF_UNIX listener under tmp_path instead "
        f"(docs/TYPING.md testing rules).")


def _is_obsidian_hostport(host, port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    return str(host or "").strip() in _LOOPBACK_HOSTS \
        and port in _OBSIDIAN_PORTS


def _no_obsidian_connect(self, addr):
    if _is_obsidian_addr(addr):
        raise _obsidian_error(addr[1])
    if _is_ydotool_socket_path(addr):
        raise _ydotool_socket_error(addr)
    return _REAL_SOCKET_CONNECT(self, addr)


def _no_obsidian_connect_ex(self, addr):
    if _is_obsidian_addr(addr):
        raise _obsidian_error(addr[1])
    if _is_ydotool_socket_path(addr):
        raise _ydotool_socket_error(addr)
    return _REAL_SOCKET_CONNECT_EX(self, addr)


def _no_obsidian_create_connection(address, *args, **kwargs):
    if _is_obsidian_addr(address):
        raise _obsidian_error(address[1])
    if _is_ydotool_socket_path(address):
        raise _ydotool_socket_error(address)
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


socket.socket.connect = _no_obsidian_connect
socket.socket.connect_ex = _no_obsidian_connect_ex
socket.create_connection = _no_obsidian_create_connection
# sendto(data, addr) on an unconnected (datagram) socket dials the path and
# puts bytes on the wire without ever calling socket.connect — guard it too.
_REAL_SOCKET_SENDTO = socket.socket.sendto


def _no_ydotool_sendto(self, data, *args, **kwargs):
    addr = kwargs.get("address", args[-1] if args else None)
    if _is_ydotool_socket_path(addr):
        raise _ydotool_socket_error(addr)
    return _REAL_SOCKET_SENDTO(self, data, *args, **kwargs)


socket.socket.sendto = _no_ydotool_sendto
# ssl.create_connection dials through socket.create_connection (guarded
# above), so it needs no separate wrapper.
import ssl as _ssl  # noqa: E402

import http.client as _http_client  # noqa: E402

_REAL_HTTP_CONN_INIT = _http_client.HTTPConnection.__init__


def _guarded_http_conn_init(self, host, port=None, *args, **kwargs):
    # HTTPSConnection subclasses HTTPConnection, so https:// too.
    if _is_obsidian_hostport(host, port):
        raise _obsidian_error(port)
    return _REAL_HTTP_CONN_INIT(self, host, port, *args, **kwargs)


_http_client.HTTPConnection.__init__ = _guarded_http_conn_init

# SSLSocket.connect/connect_ex are overrides (TLS handshake wrapping) that
# reach the wire without passing the patched socket.socket methods on every
# Python version — guard the class itself.
_REAL_SSL_CONNECT = _ssl.SSLSocket.connect
_REAL_SSL_CONNECT_EX = _ssl.SSLSocket.connect_ex


def _no_obsidian_ssl_connect(self, addr):
    if _is_obsidian_addr(addr):
        raise _obsidian_error(addr[1])
    return _REAL_SSL_CONNECT(self, addr)


def _no_obsidian_ssl_connect_ex(self, addr):
    if _is_obsidian_addr(addr):
        raise _obsidian_error(addr[1])
    return _REAL_SSL_CONNECT_EX(self, addr)


_ssl.SSLSocket.connect = _no_obsidian_ssl_connect
_ssl.SSLSocket.connect_ex = _no_obsidian_ssl_connect_ex

# An ambient YDOTOOL_SOCKET from the owner's session must not steer tests
# into probing their real daemon. XDG_RUNTIME_DIR likewise: the resolver
# falls back to $XDG_RUNTIME_DIR/.ydotool_socket, and the owner's value
# (/run/user/<uid>) points straight at the live socket.
os.environ.pop("YDOTOOL_SOCKET", None)
os.environ.pop("XDG_RUNTIME_DIR", None)


@pytest.fixture(autouse=True)
def _clean_session_env():
    """Re-assert #1 per test: whoever set DISPLAY/WAYLAND_DISPLAY/... must
    not leak it to the next test (monkeypatch restores what it set, and
    what conftest popped is gone for good)."""
    for _v in ("HYPRLAND_INSTANCE_SIGNATURE", "HYPRLAND_CMD",
               "WAYLAND_DISPLAY", "DISPLAY", "SWAYSOCK",
               "DBUS_SESSION_BUS_ADDRESS", "YDOTOOL_SOCKET",
               "XDG_RUNTIME_DIR"):
        os.environ.pop(_v, None)
    yield
    for _v in ("HYPRLAND_INSTANCE_SIGNATURE", "HYPRLAND_CMD",
               "WAYLAND_DISPLAY", "DISPLAY", "SWAYSOCK",
               "DBUS_SESSION_BUS_ADDRESS", "YDOTOOL_SOCKET",
               "XDG_RUNTIME_DIR"):
        os.environ.pop(_v, None)


HUB_KEY = "test-hub-key"
CHAT_REPLY = "[{name}] you said: {text}"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wav_bytes(seconds=0.2, rate=24000):
    """A tiny valid mono 16-bit WAV (what breeze/hub hand out, just small)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x20\x00\x00\x01" * int(rate * seconds))
    return buf.getvalue()


AUDIO_WAV = wav_bytes()


def persona(pid, name, keyword, greeting, tts_voice=""):
    return {"id": pid, "name": name, "keywords": [keyword], "system_prompt": f"You are {name}.",
            "greeting": greeting, "tts_voice": tts_voice, "avatar_version": 1,
            "voice": {"mode": "design", "description": f"{name}'s designed voice.",
                      "style": "", "ref_text": "", "has_reference": False,
                      "seed": 1234, "cfg_scale": None},
            "voice_version": 1,
            "updated_at": "2026-11-03T00:00:00Z"}


def avatar_bytes(pid, version):
    return b"glTF" + f"-avatar-{pid}-v{version}-".encode() + b"\0" * 32


class FakeHub:
    """Stand-in for hub/server.py: personas, keyword switching, history, import,
    avatars, auth, hub-side voice (docs/VOICE.md), a state version counter and
    the client-tool channel + events long-poll (docs/API.md) — the contract
    the clients use."""

    SWITCH = re.compile(r"^(switch to|change to|become|persona|talk to|i want to talk to)\s+(.+)$")

    def __init__(self, api_key=HUB_KEY):
        self.api_key = api_key
        self.lock = threading.Lock()
        self.version = 1
        self.message_id = 0
        self.messages = []            # Message dicts, global order
        self.avatar_requests = []     # (id, version) of every avatar GET
        self.chat_requests = []       # JSON body of every /api/chat
        self.audio_requests = []      # path of every audio GET (or /api/tts body)
        self.emotion = None           # when set, chat replies carry this ChatResult.emotion
        self.actions = None           # when set, chat replies carry this ChatResult.actions
        self.speech = None            # when set, chat replies carry this ChatResult.speech
        self.register_count = 0       # how many register POSTs arrived
        self.last_auth = None         # Authorization header of the last API call
        self.read_requests = []       # JSON body of every POST /api/chat/read (docs/TIMERS.md)
        self.has_chat_read = True     # False -> an OLD hub: POST /api/chat/read is 404
        self.has_events = True        # False -> an OLD hub: GET /api/events is 404
        # Client-tool channel (docs/API.md), server side, simplified:
        self.registered = {}          # client_id -> last register body
        self.heartbeats = []          # client_id of every heartbeat POST
        self.results = []             # JSON body of every /results POST
        self.unregistered = []        # client_ids that DELETEd themselves
        self._queued = {}             # client_id -> list of tool requests
        self._cond = threading.Condition()
        # Events ring (GET /api/events):
        self.events = []              # {seq, time, type, ...}
        self.event_seq = 0
        # Voice knobs the tests flip:
        self.tts_enabled = True       # False -> every audio_url is null
        self.tts_reachable = True     # GET /api/tts/status reports this
        self.has_tts_status = True    # False -> an OLD hub: /api/tts/status is 404
        self.audio_mode = "ok"        # "ok" | "error" (503) | "missing" (404)
        self.personas = {
            "riko": persona("riko", "Riko", "riko", "Riko here. Miss me, senpai?"),
            "asuka": persona("asuka", "Asuka", "asuka", "Asuka here, senpai.", "af_sleepy"),
        }
        self.active = "riko"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # no keep-alive: stop() really ends the conversation

            def _send(self, payload, status=200, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json_body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _query(self):
                return urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                             if "?" in self.path else "")

            def _audio(self):
                """Reply for any audio GET / POST /api/tts, honouring audio_mode."""
                if outer.audio_mode == "error":
                    return self._send({"detail": "breeze is unreachable"}, 503)
                if outer.audio_mode == "missing":
                    return self._send({"detail": "no such audio"}, 404)
                return self._send(AUDIO_WAV, content_type="audio/wav")

            def _authed(self):
                outer.last_auth = self.headers.get("Authorization")
                if outer.api_key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {outer.api_key}"

            def do_GET(self):
                path = self.path.split("?")[0]
                if path != "/api/health" and not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                if path == "/api/health":
                    return self._send({"ok": True, "version": "1"})
                if path == "/api/state":
                    return self._send(outer.state())
                if path == "/api/personas":
                    return self._send(list(outer.personas.values()))
                if m := re.fullmatch(r"/api/personas/([\w-]+)", path):
                    p = outer.personas.get(m[1])
                    return self._send(p) if p else self._send({"detail": "no such persona"}, 404)
                if m := re.fullmatch(r"/api/personas/([\w-]+)/avatar", path):
                    p = outer.personas.get(m[1])
                    if not p:
                        return self._send({"detail": "no such persona"}, 404)
                    outer.avatar_requests.append((p["id"], p["avatar_version"]))
                    return self._send(avatar_bytes(p["id"], p["avatar_version"]),
                                      content_type="model/gltf-binary")
                if path == "/api/history":
                    return self._send({"persona": outer.active, "messages": list(outer.messages)})
                if path == "/api/tts/status":
                    if not outer.has_tts_status:
                        return self._send({"detail": "Not Found"}, 404)  # old hub, no voice
                    return self._send({"enabled": outer.tts_enabled,
                                       "reachable": outer.tts_reachable,
                                       "base_url": "http://fake-breeze:8137", "voices": 1})
                if m := re.fullmatch(r"/api/messages/(\d+)/audio", path):
                    outer.audio_requests.append(self.path)  # raw: keep any ?v=
                    msg = next((x for x in outer.messages if x["id"] == int(m[1])), None)
                    if not msg or msg["role"] != "assistant":
                        return self._send({"detail": "no such message"}, 404)
                    return self._audio()
                if m := re.fullmatch(r"/api/personas/([\w-]+)/greeting/audio", path):
                    outer.audio_requests.append(self.path)  # raw: keep any ?v=
                    p = outer.personas.get(m[1])
                    if not p:
                        return self._send({"detail": "no such persona"}, 404)
                    if (p.get("voice") or {}).get("mode") == "off":
                        return self._send({"detail": "persona voice is off"}, 409)
                    return self._audio()
                if path == "/api/events":
                    if not outer.has_events:
                        return self._send({"detail": "Not Found"}, 404)  # old hub, no events
                    qp = self._query()
                    after = int(qp.get("after", ["0"])[0] or 0)
                    wait = min(float(qp.get("wait", ["0"])[0] or 0), 0.5)
                    deadline = time.monotonic() + wait
                    with outer._cond:
                        while not any(e["seq"] > after for e in outer.events) \
                                and time.monotonic() < deadline:
                            outer._cond.wait(0.05)
                        out = [e for e in outer.events if e["seq"] > after]
                    return self._send({"events": out,
                                       "last": max([e["seq"] for e in out], default=after)})
                if path == "/api/clients":
                    return self._send({"clients": [
                        {"client_id": cid, "tools": body.get("tools", [])}
                        for cid, body in outer.registered.items()],
                        "heartbeat_s": 30})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/requests", path):
                    cid = m[1]
                    wait = min(float(self._query().get("wait", ["25"])[0] or 25), 0.5)
                    with outer._cond:
                        if cid not in outer.registered:
                            return self._send({"detail": "register first"}, 404)
                        if not outer._queued.get(cid):
                            outer._cond.wait(wait)
                        out = outer._queued.pop(cid, [])
                    return self._send({"requests": out, "approval_timeout_s": 120})
                self._send({"detail": "not found"}, 404)

            def do_POST(self):
                if not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                body = self._json_body()
                if self.path == "/api/active":
                    p = outer.find(str(body.get("persona", "")))
                    if not p:
                        return self._send({"detail": "no such persona"}, 404)
                    with outer.lock:
                        outer.active = p["id"]
                        outer.version += 1
                    return self._send(outer.state())
                if self.path == "/api/chat":
                    return self._send(outer.chat(body))
                if self.path == "/api/chat/read":
                    if not outer.has_chat_read:
                        return self._send({"detail": "Not Found"}, 404)  # old hub, no timers
                    outer.read_requests.append(body)
                    return self._send({"ok": True, "cancelled": []})
                if self.path == "/api/tts":
                    outer.audio_requests.append(("tts", body.get("text", "")))
                    return self._audio()
                if self.path == "/api/history/import":
                    pid = body.get("persona") or outer.active
                    n = 0
                    with outer.lock:
                        for m in body.get("messages", []):
                            if m.get("role") not in ("user", "assistant"):
                                return self._send({"detail": "bad role"}, 422)
                            outer.message_id += 1
                            outer.messages.append({"id": outer.message_id, "persona": pid,
                                                   "role": m["role"], "content": m["content"],
                                                   "source": "import",
                                                   "created_at": "2026-11-03T00:00:00Z"})
                            n += 1
                        outer.version += 1
                    return self._send({"imported": n})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/register", self.path):
                    tools = body.get("tools")
                    if not isinstance(tools, list):
                        return self._send({"detail": "tools must be a list"}, 422)
                    for t in tools:
                        if not isinstance(t, dict) or not re.fullmatch(
                                r"[A-Za-z0-9_-]{1,64}", str(t.get("name") or "")):
                            return self._send({"detail": "bad tool name"}, 422)
                        if t.get("risk", "read") not in ("read", "act", "danger"):
                            return self._send({"detail": "bad tool risk"}, 422)
                    with outer._cond:
                        outer.registered[m[1]] = body
                        outer.register_count += 1
                        outer._cond.notify_all()
                    return self._send({"ok": True, "heartbeat_s": 30})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/heartbeat", self.path):
                    if m[1] not in outer.registered:
                        return self._send({"detail": "register first"}, 404)
                    outer.heartbeats.append(m[1])
                    return self._send({"ok": True, "heartbeat_s": 30})
                if m := re.fullmatch(r"/api/clients/([\w-]+)/results", self.path):
                    if not body.get("request_id"):
                        return self._send({"detail": "request_id required"}, 422)
                    outer.results.append(body)
                    return self._send({"ok": True})
                self._send({"detail": "not found"}, 404)

            def do_PUT(self):
                if not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                m = re.fullmatch(r"/api/personas/([\w-]+)", self.path)
                p = outer.personas.get(m[1]) if m else None
                if not p:
                    return self._send({"detail": "no such persona"}, 404)
                body = self._json_body()
                with outer.lock:
                    for k in ("name", "system_prompt", "keywords", "greeting", "tts_voice"):
                        if k in body:
                            p[k] = body[k]
                    outer.version += 1
                return self._send(p)

            def do_DELETE(self):
                if not self._authed():
                    return self._send({"detail": "invalid api key"}, 401)
                if self.path.split("?")[0] == "/api/history":
                    with outer.lock:
                        outer.messages = []
                        outer.version += 1
                    return self._send(b"", 204)
                if m := re.fullmatch(r"/api/clients/([\w-]+)", self.path.split("?")[0]):
                    with outer._cond:
                        outer.registered.pop(m[1], None)
                        outer.unregistered.append(m[1])
                        outer._cond.notify_all()
                    return self._send(b"", 204)
                self._send({"detail": "not found"}, 404)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    # ── helpers ──────────────────────────────────────────────
    def state(self):
        with self.lock:
            return {"version": self.version, "active": self.personas[self.active],
                    "personas": [{"id": p["id"], "name": p["name"]} for p in self.personas.values()],
                    "last_message_id": self.message_id, "llm_model": "test-model"}

    def find(self, needle):
        needle = (needle or "").strip().lower()
        for p in self.personas.values():
            if needle in (p["id"], p["name"].lower(), *[k.lower() for k in p["keywords"]]):
                return p
        return None

    def chat(self, body):
        text = str(body.get("text", "")).strip().lower().rstrip(".!?,")
        self.chat_requests.append(body)
        target = None
        with self.lock:
            if text in self.personas or self.find(text):
                target = self.find(text)
            else:
                m = self.SWITCH.match(text)
                if m:
                    target = self.find(m[2])
            if target:
                self.active = target["id"]
                self.version += 1
                greeting_audio = self._greeting_audio_url(target)
                resp = {"reply": target["greeting"] or f"{target['name']} here.",
                        "persona": target["id"], "switched": True,
                        "message_ids": [], "state_version": self.version,
                        "audio_url": greeting_audio}
                if self.emotion:  # tests can set .emotion to mimic an emotion-aware hub
                    resp["emotion"] = self.emotion
                if self.actions is not None:  # body-action-aware hub
                    resp["actions"] = self.actions
                if self.speech is not None:  # spoken-text-aware hub (hub/speech.py)
                    resp["speech"] = self.speech
                return resp
            pid = self.active
            ids = []
            for role, content in (("user", body.get("text")), ("assistant",
                                 CHAT_REPLY.format(name=self.personas[pid]["name"],
                                                   text=body.get("text")))):
                self.message_id += 1
                ids.append(self.message_id)
                self.messages.append({"id": self.message_id, "persona": pid, "role": role,
                                      "content": content, "source": body.get("source", "test"),
                                      # a voice-aware hub stores the 🎤 flag (docs/API.md)
                                      "voice": bool(body.get("voice")) and role == "user",
                                      "created_at": "2026-11-03T00:00:00Z"})
            self.version += 1
            audio_url = None
            if self.tts_enabled:
                audio_url = f"/api/messages/{ids[-1]}/audio"
            resp = {"reply": CHAT_REPLY.format(name=self.personas[pid]["name"],
                                               text=body.get("text")),
                    "persona": pid, "switched": False,
                    "message_ids": ids, "state_version": self.version,
                    "audio_url": audio_url}
            if self.emotion:
                resp["emotion"] = self.emotion
            if self.actions is not None:  # body-action-aware hub
                resp["actions"] = self.actions
            if self.speech is not None:  # spoken-text-aware hub (hub/speech.py)
                resp["speech"] = self.speech
            return resp

    def _greeting_audio_url(self, p):
        """ChatResult.audio_url for a switch (docs/VOICE.md), null with TTS
        turned off or the persona's voice set to off."""
        if not self.tts_enabled or (p.get("voice") or {}).get("mode") == "off":
            return None
        return f"/api/personas/{p['id']}/greeting/audio?v={p.get('voice_version', 1)}"

    def force_active(self, pid):
        """A switch made by some other client (web page, another PC)."""
        with self.lock:
            self.active = pid
            self.version += 1

    def bump_avatar(self, pid):
        with self.lock:
            self.personas[pid]["avatar_version"] += 1
            self.version += 1

    # ── client-tool channel test helpers ─────────────────────
    def push_request(self, client_id, tool, arguments=None, needs_approval=False,
                     summary=None, request_id=None):
        """Queue a tool call for a client, like the real dispatch() does."""
        req = {"request_id": request_id or f"req-{tool}-{len(self.results)}",
               "tool": tool, "arguments": arguments or {},
               "needs_approval": bool(needs_approval),
               "summary": summary or f"{tool} {json.dumps(arguments or {})}"}
        with self._cond:
            self._queued.setdefault(client_id, []).append(req)
            self._cond.notify_all()
        return req

    def push_event(self, etype, **fields):
        """Append one event to the GET /api/events ring."""
        with self._cond:
            self.event_seq += 1
            ev = {"seq": self.event_seq, "time": "2026-11-03T00:00:00Z",
                  "type": etype, **fields}
            self.events.append(ev)
            self._cond.notify_all()
        return ev

    def wait_for_register(self, client_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if client_id in self.registered:
                return True
            time.sleep(0.05)
        return False

    def wait_for_result(self, request_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in self.results:
                if r.get("request_id") == request_id:
                    return r
            time.sleep(0.05)
        return None

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        with self._cond:  # wake any half-open long-polls so shutdown is quick
            self._cond.notify_all()


def make_cfg(hub_url):
    """A client config pointing at a fake hub; no mic or TTS involved."""
    return {
        "hub": {"mode": "remote", "url": hub_url, "api_key_env": "FAKE_HUB_KEY"},
        "llm": {"base_url": "http://127.0.0.1:1/v1", "api_key_env": "TEST_NO_SUCH_KEY",
                "model": "auto", "temperature": 0.8, "max_tokens": 1024, "max_history": 40},
        "asr": "remote",
        "remote_asr": {},
        "local_asr": {},
        "tts": "none",
        "openai_tts": {"url": "http://127.0.0.1:1/v1", "api_key_env": "TTS_API_KEY",
                       "model": "kokoro", "voice": "af_heart"},
        "sovits": {}, "fish": {},
        "desktop": {"width": 420, "height": 680, "bubble_seconds": 8},
    }


@pytest.fixture
def fake_hub():
    hub = FakeHub()
    yield hub
    hub.stop()


@pytest.fixture(autouse=True)
def clean_hub_client_globals():
    """hub_client keeps a module-level client + active persona; never leak them."""
    from riko import hub_client
    hub_client._reset()
    yield
    hub_client._reset()


@pytest.fixture
def hub_connected(fake_hub, monkeypatch, tmp_path):
    """A connected hub_client (the way check() leaves it), avatars cached in tmp."""
    from riko import hub_client
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    client = hub_client.connect(make_cfg(fake_hub.url))
    hub_client.note_active(client.state())
    return client
