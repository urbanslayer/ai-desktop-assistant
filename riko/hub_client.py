"""Her brain now lives on the hub: this is the one client every Riko app talks to.

The hub (docs/DESIGN.md) keeps the personas, the chat history and the LLM
settings, so the desktop app, the terminal and any future app share one head.
Two ways to run it, same code path (we always talk HTTP):

    hub.mode: local   the hub runs inside this process (needs the hub/ package),
                      on 127.0.0.1:<hub.local_port>, data in <hub.local_data>
    hub.mode: remote  a hub on another box (Docker), at hub.url

The bearer key comes from the .env variable named in hub.api_key_env.
riko/llm.py used to do the thinking here; that logic is the hub's now.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import requests

from riko.config import ROOT

CHAT_TIMEOUT = 300     # a turn may run tools for minutes (docs/API.md Tools)
DEFAULT_TIMEOUT = 15
AVATAR_TIMEOUT = 120   # a .vrm can be tens of MB
AUDIO_TIMEOUT = 120    # the hub may still be synthesising when we ask
LONGPOLL_TIMEOUT = 70  # GET /api/events or /clients/{id}/requests: wait + slack


def avatar_cache_dir():
    """Where persona avatars are cached, by id + avatar_version.

    ~/.cache on purpose: nothing under the project folder is git-ignored
    except the paths already listed there. RIKO_AVATAR_CACHE overrides it.
    """
    d = os.environ.get("RIKO_AVATAR_CACHE")
    return Path(d) if d else Path.home() / ".cache" / "riko" / "avatars"


class HubError(RuntimeError):
    """The hub answered, and the answer was an error.

    `status` carries the HTTP code when there was one (None for transport
    errors), so callers can tell e.g. 404 = old hub without this route.
    """

    def __init__(self, *args, status=None):
        super().__init__(*args)
        self.status = status


class HubAuthError(HubError):
    """The hub rejected our bearer key (401)."""


class HubUnreachable(HubError):
    """The hub is not answering at all (down, wrong url, network)."""


class HubClient:
    def __init__(self, url, api_key="", cache_dir=None):
        self.url = str(url).rstrip("/")
        self.api_key = api_key or ""
        self.cache_dir = Path(cache_dir) if cache_dir else avatar_cache_dir()
        self.http = requests.Session()

    # ── Plumbing ─────────────────────────────────────────────
    def _call(self, method, path, timeout=DEFAULT_TIMEOUT, **kw):
        headers = kw.pop("headers", {})
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.http.request(method, self.url + path, timeout=timeout,
                                         headers=headers, **kw)
        except requests.RequestException as e:
            raise HubUnreachable(f"can't reach the hub at {self.url} ({type(e).__name__})") from e
        if response.status_code == 401:
            raise HubAuthError(
                f"the hub at {self.url} rejected the key (401 invalid api key)")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text[:200])
            except ValueError:
                detail = response.text[:200] or response.reason
            raise HubError(f"{method} {path} -> {response.status_code}: {detail}",
                           status=response.status_code)
        return response

    def _json(self, method, path, **kw):
        return self._call(method, path, **kw).json()

    # ── The API (docs/DESIGN.md) ─────────────────────────────
    def health(self):
        return self._json("GET", "/api/health", timeout=5)

    def state(self):
        return self._json("GET", "/api/state")

    def personas(self):
        return self._json("GET", "/api/personas")

    def persona(self, persona_id):
        return self._json("GET", f"/api/personas/{persona_id}")

    def update_persona(self, persona_id, fields):
        return self._json("PUT", f"/api/personas/{persona_id}", json=fields)

    def set_active(self, name_or_id):
        """Switch persona by id, name or keyword (case-insensitive, hub-side)."""
        return self._json("POST", "/api/active", json={"persona": name_or_id})

    def persona_animations(self, persona_id):
        """Custom keyframe animations (docs/SELF_ANIMATION.md) the owner made
        for this persona: current model, every upload status the hub serves.
        Returns the list (possibly empty); the hub owns validation."""
        data = self._json("GET",
                          f"/api/personas/{persona_id}/animations"
                          f"?model=current&status=all")
        anims = data.get("animations") if isinstance(data, dict) else data
        return [a for a in (anims or []) if isinstance(a, dict)]

    def chat(self, text, source="desktop", persona=None, voice=False):
        """One turn. Returns the ChatResult: {reply, speech, persona, switched,
        message_ids, state_version}. `speech` is the cleaned spoken text
        (docs/API.md): speak it, display `reply`. A keyword turn (e.g.
        'switch to asuka') comes back as switched=true with the persona's
        greeting as reply. `voice=True` marks the text as speech-recognised
        (docs/API.md): the hub adds a never-comment-on-misheard-names line
        to the system prompt and stores a 🎤 flag on the user message;
        hubs before this field simply ignore it."""
        body = {"text": text, "source": source}
        if persona:
            body["persona"] = persona
        if voice:
            body["voice"] = True
        return self._json("POST", "/api/chat", json=body, timeout=CHAT_TIMEOUT)

    def touch(self, kind, part=None, hold_ms=None, persona=None, sitting=None):
        """One physical touch from the owner's mouse (docs/TOUCH.md).
        Returns {reacted, spoke, text, emotion, actions, audio_url, reason,
        detail, state_version}. Fast (< 50 ms) whenever the hub decides not
        to speak; only the spoken path waits on the LLM, so run this off the
        UI thread. kind ∈ pat|poke|tickle|grab|hold|release|action|sit|stand."""
        body = {"kind": kind}
        if part:
            body["part"] = part
        if hold_ms is not None:
            body["hold_ms"] = hold_ms
        if persona:
            body["persona"] = persona
        if sitting is not None:
            body["sitting"] = bool(sitting)
        return self._json("POST", "/api/touch", json=body, timeout=CHAT_TIMEOUT)

    def settings(self):
        """GET /api/settings (None on a hub too old for the route)."""
        try:
            return self._json("GET", "/api/settings")
        except HubError as e:
            if e.status == 404:
                return None
            raise

    def save_touch_mode(self, mode):
        """PUT /api/settings {"touch": {"mode": ...}} — the tray's Touch
        reactions writes the SAME block the web Settings card edits
        (docs/TOUCH.md). Returns the merged settings, or None on an old hub."""
        try:
            return self._json("PUT", "/api/settings", json={"touch": {"mode": mode}})
        except HubError as e:
            if e.status == 404:
                return None
            raise

    def history(self, persona=None, after=0, limit=50):
        params = {"after": after, "limit": limit}
        if persona:
            params["persona"] = persona
        return self._json("GET", "/api/history", params=params)

    def clear_history(self, persona=None):
        params = {"persona": persona} if persona else {}
        self._call("DELETE", "/api/history", params=params)

    def import_history(self, persona, messages, replace=False):
        """Feed old {role, content} messages in once; returns how many landed."""
        result = self._json("POST", "/api/history/import",
                            json={"persona": persona, "messages": messages, "replace": replace})
        return int(result.get("imported", 0))

    # ── Events + the client-tool channel (docs/API.md, R1-B/R2-F) ──
    def events(self, after=0, wait=25):
        """Long-poll GET /api/events -> {events: [...], last}. Pass `last`
        back as `next after`; an empty list just means 'nothing yet'."""
        return self._json("GET", "/api/events",
                          params={"after": after, "wait": wait},
                          timeout=LONGPOLL_TIMEOUT)

    def clients(self):
        """GET /api/clients: who is online on the client-tool channel."""
        return self._json("GET", "/api/clients")

    def client_register(self, client_id, name, kind="desktop", platform="",
                        tools=(), body=None):
        """Publish this app's tools; the model sees them as <kind>__<name>.

        `body` (docs/BODY.md §1) is an optional {actions: [...], expressions:
        [...]} of gesture/expression names this client can play right now.
        """
        body_json = {"name": name, "kind": kind, "tools": list(tools)}
        if platform:
            body_json["platform"] = platform
        if body is not None:
            body_json["body"] = body
        return self._json("POST", f"/api/clients/{client_id}/register", json=body_json)

    def client_heartbeat(self, client_id):
        return self._json("POST", f"/api/clients/{client_id}/heartbeat", timeout=10)

    def client_requests(self, client_id, wait=25):
        """Long-poll for tool calls to run: {requests: [...], approval_timeout_s}."""
        return self._json("GET", f"/api/clients/{client_id}/requests",
                          params={"wait": wait}, timeout=LONGPOLL_TIMEOUT)

    def client_result(self, client_id, request_id, ok=True, result=None,
                      error="", approved=None):
        body = {"request_id": request_id, "ok": bool(ok), "error": error or ""}
        if result is not None:
            body["result"] = result
        if approved is not None:
            body["approved"] = bool(approved)
        return self._json("POST", f"/api/clients/{client_id}/results", json=body)

    def client_unregister(self, client_id):
        self._call("DELETE", f"/api/clients/{client_id}")

    def chat_read(self, persona_id):
        """POST /api/chat/read (docs/TIMERS.md): the owner saw this persona's
        chat, so the hub cancels that persona's chat.read timers. False on an
        old hub without the route (404 — silently nothing happened)."""
        try:
            self._call("POST", "/api/chat/read", json={"persona_id": persona_id})
            return True
        except HubError as e:
            if e.status == 404:  # old hub without timers: nothing to cancel
                return False
            raise

    # ── Voice (the hub makes the audio; docs/VOICE.md) ───────
    def tts_status(self):
        """GET /api/tts/status -> {enabled, reachable, base_url, voices}.

        None when the hub is too old to have the route (404) — old hub,
        no hub-side voice, clients fall back to their own TTS or bubbles.
        """
        try:
            return self._json("GET", "/api/tts/status")
        except HubError as e:
            if e.status == 404:
                return None
            raise

    def get_audio(self, audio_url):
        """WAV bytes for a ChatResult's audio_url (a hub path like
        /api/messages/42/audio or /api/personas/<id>/greeting/audio?v=3)."""
        return self._call("GET", audio_url, timeout=AUDIO_TIMEOUT).content

    def tts_audio(self, text, persona=None):
        """POST /api/tts: WAV bytes of `text` in a persona's voice (no cache)."""
        body = {"text": text}
        if persona:
            body["persona"] = persona
        return self._call("POST", "/api/tts", json=body, timeout=AUDIO_TIMEOUT).content

    # ── Avatars ──────────────────────────────────────────────
    def avatar_file(self, persona_id, avatar_version):
        """The active persona's .vrm on disk, downloaded once per id+version."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{persona_id}-v{avatar_version}.vrm"
        if not path.exists():
            data = self._call("GET", f"/api/personas/{persona_id}/avatar",
                              timeout=AVATAR_TIMEOUT).content
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)  # never leave a half-written .vrm behind
        return path


# ── The one client every module in the app shares ────────────
_client = None
_active = None  # last-seen active Persona (id, name, avatar_version, tts_voice, ...)
_names_cache = None  # (expiry_monotonic, [persona names]) — see persona_names()
PERSONA_NAMES_TTL = 300.0  # names change rarely; the ASR prompt asks at most every 5 min


def _reset():
    global _client, _active, _names_cache, _custom_animations, _on_custom_animations
    _client = None
    _active = None
    _names_cache = None
    _custom_animations = []  # custom keyframe list cache (SELF_ANIMATION.md)
    _on_custom_animations = None
    _last_chat_read.clear()  # tests must not inherit another test's chat.read throttle


def get():
    if _client is None:
        raise HubError("the hub client was never set up: call hub_client.check(cfg) at startup")
    return _client


def connect(cfg):
    """Build the client from cfg['hub'] (no network yet)."""
    global _client
    hub = cfg.get("hub") or {}
    mode = hub.get("mode") or "local"
    key = os.getenv(hub.get("api_key_env") or "", "")
    if mode == "local":
        url = f"http://127.0.0.1:{int(hub.get('local_port') or 8002)}"
    else:
        url = hub.get("url") or ""
    _client = HubClient(url, key)
    return _client


def note_active(state):
    """Remember the active persona from a State answer (no UI events)."""
    global _active, _names_cache
    new = state.get("active") if isinstance(state, dict) else None
    if (new or {}).get("id") != (_active or {}).get("id"):
        _names_cache = None  # persona_names() puts the ACTIVE persona first
    _active = new


def active_persona():
    return _active


def persona_names():
    """Persona names from the hub — the ACTIVE persona first, then the rest —
    for riko.asr's vocabulary prompt. Cached ~5 min (names change rarely;
    this is asked once per recorded clip). Never raises: no client yet or a
    hub failure returns the stale cache, or [] the first time."""
    global _names_cache
    now = time.monotonic()
    if _names_cache is not None and now < _names_cache[0]:
        return list(_names_cache[1])
    try:
        listed = get().personas()
        names = [str(p["name"]).strip() for p in listed
                 if isinstance(p, dict) and str(p.get("name") or "").strip()]
        active_id = (_active or {}).get("id")
        active_name = next((str(p["name"]).strip() for p in listed
                            if isinstance(p, dict) and p.get("id") == active_id), "")
        if active_name:  # Whisper biases the prompt words it sees first hardest
            names = [active_name] + [n for n in names if n.lower() != active_name.lower()]
    except Exception:
        return list(_names_cache[1]) if _names_cache else []
    _names_cache = (now + PERSONA_NAMES_TTL, names)
    return list(names)


def announce_persona(state):
    """Tell the avatar page to swap to the active persona when it changed
    (by keyword, riko-ctl, the web page, another PC...). True if it fired."""
    active = (state or {}).get("active") or {}
    changed = (_active is None
               or active.get("id") != (_active or {}).get("id")
               or active.get("avatar_version") != (_active or {}).get("avatar_version"))
    note_active(state)
    if not changed:
        return False
    from riko import viewer  # lazy: viewer imports this module at startup
    viewer.emit("persona", id=active["id"], name=active["name"],
                avatar_version=active["avatar_version"])
    # new persona, new custom animation list (docs/SELF_ANIMATION.md) —
    # this runs on the hub poller / switch caller's thread, never Qt
    refresh_custom_animations()
    return True


# ── custom keyframe animations (docs/SELF_ANIMATION.md) ───────────
# The desktop is the cache: it fetches the active persona's list from the
# hub at startup, on every persona switch and on the hub's
# `animations_changed` event. The avatar page pulls the cache through the
# viewer (GET /custom-animations.json) and reloads on `custom-animations:reload`.
_custom_animations: list = []
_on_custom_animations = None  # desktop.py sets fn(count); fires after a refresh


def set_on_custom_animations(fn):
    global _on_custom_animations
    _on_custom_animations = fn


def custom_animations():
    return list(_custom_animations)


def refresh_custom_animations():
    """Fetch the active persona's custom animations into the cache and tell
    the page to re-read it. Runs on whatever thread the HUB calls come from
    (startup helper thread, state poller, events follower) — never the Qt
    thread. A hub failure keeps the previous cache. Returns True on success."""
    global _custom_animations
    active = _active or {}
    pid = active.get("id")
    if not pid:
        return False
    try:
        anims = get().persona_animations(pid)
    except Exception as e:  # HubError etc: hub down or hub without the endpoint
        print(f"[animations] fetch failed: {type(e).__name__}: {e} "
              f"(keeping {len(_custom_animations)} cached)")
        return False
    _custom_animations = anims
    print(f"[animations] {len(anims)} custom animation(s) for persona {pid}")
    if _on_custom_animations:
        try:
            _on_custom_animations(len(anims))
        except Exception as e:
            print(f"[animations] notify failed: {type(e).__name__}: {e}")
    return True


def start_state_poller(client=None, interval=3.0):
    """Watch the hub every few seconds, so switches made elsewhere land here.
    A briefly-down hub is logged once, not spammed."""
    client = client or get()

    def loop():
        down = False
        while True:
            time.sleep(interval)
            try:
                state = client.state()
            except HubError as e:
                if not down:  # the hub may be restarting; say it once and keep trying
                    print(f"[hub] {e} (retrying quietly)")
                    down = True
                continue
            if down:
                print("[hub] the hub is back")
                down = False
            announce_persona(state)

    threading.Thread(target=loop, daemon=True, name="riko-hub-poll").start()


def start_events_follower(on_event, client=None, wait=25.0, backoff_s=2.0,
                          stop=None):
    """Follow GET /api/events (docs/API.md) on a daemon thread.

    One long-poll for every live-event consumer in the app (tool progress,
    timer turns — docs/TIMERS.md D). The cursor is primed SYNCHRONOUSLY here
    (one wait=0 poll) so nothing buffered before app start is ever replayed,
    and every event fired after this call returns is guaranteed delivered.
    A hub without the route answers 404: the follower never starts and the
    returned stop Event is already set — old hubs simply have no live events.
    A down hub retries every `backoff_s`. `on_event(kind, event)` runs on
    the follower thread; exceptions there are printed, never fatal. Returns
    the stop Event (set it to end the follower).
    """
    client = client or get()
    stop = stop if stop is not None else threading.Event()
    cursor = 0
    try:  # prime: skip everything the hub buffered before we started
        cursor = int(client.events(after=0, wait=0).get("last") or 0)
    except HubError as e:
        if getattr(e, "status", None) == 404:
            stop.set()  # old hub, no events route: nothing to follow
            return stop
        # briefly-down hub at startup: the loop below retries with backoff

    def loop():
        nonlocal cursor
        while not stop.is_set():
            try:
                out = client.events(after=cursor, wait=int(wait))
            except HubError as e:
                if getattr(e, "status", None) == 404:  # hub lost the route
                    return
                stop.wait(backoff_s)  # hub down/restarting: back off, keep up
                continue
            cursor = int(out.get("last") or cursor)
            for ev in out.get("events") or []:
                try:
                    on_event(str(ev.get("type") or ""), ev)
                except Exception as e:  # one bad handler must not kill the stream
                    print(f"[events] handler: {type(e).__name__}: {e}")

    threading.Thread(target=loop, daemon=True, name="riko-events").start()
    return stop


# docs/TIMERS.md D: POST /api/chat/read when the owner sees a persona's chat
# (window shown/focused). At most once per 30 s per persona.
CHAT_READ_THROTTLE_S = 30.0
_last_chat_read = {}  # persona_id -> time.monotonic() of the last attempt


def note_chat_read(persona_id=None, client=None, throttle_s=None):
    """Tell the hub the owner looked at this persona's chat (docs/TIMERS.md):
    the hub cancels that persona's chat.read timers. Called on window
    show/focus. Never raises and never blocks: the POST runs on a daemon
    thread, an old hub's 404 is ignored silently, and each persona is
    reported at most once per `throttle_s` (default 30)."""
    client = client or _client
    if client is None:
        return
    if persona_id is None:
        persona_id = (_active or {}).get("id")
    if not persona_id:
        return
    limit = CHAT_READ_THROTTLE_S if throttle_s is None else float(throttle_s)
    now = time.monotonic()
    if now - _last_chat_read.get(persona_id, float("-inf")) < limit:
        return
    _last_chat_read[persona_id] = now  # count the ATTEMPT: a down hub must not stampede

    def go():
        try:
            client.chat_read(persona_id)  # 404 (old hub) returns False inside
        except HubError as e:
            print(f"[hub] chat/read report failed: {e}")

    threading.Thread(target=go, daemon=True, name="riko-chat-read").start()


# ── Startup ───────────────────────────────────────────────────
def _llm_defaults(cfg):
    """The llm: section is the seed the local hub starts from (its own store wins after)."""
    llm = cfg.get("llm") or {}
    defaults = {k: llm[k] for k in ("base_url", "model", "temperature", "max_tokens",
                                    "max_history", "reasoning_effort", "extra_body")
                if llm.get(k) is not None}
    defaults["api_key"] = os.getenv(llm.get("api_key_env") or "", "")
    return defaults


def _start_local(cfg, client):
    try:
        from hub.server import create_app
    except ImportError:
        sys.exit("[setup] hub.mode: local needs the hub package (hub/server.py), which this "
                 "folder doesn't have.\n"
                 "        Run the hub somewhere (hub/ has a Docker compose file) and set\n"
                 "        hub.mode: remote plus hub.url in character_config.yaml.")
    port = int((cfg.get("hub") or {}).get("local_port") or 8002)
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        sys.exit(f"[setup] Port {port} is busy (another Riko or hub already running there?). "
                 f"Close it, or change hub.local_port, or set hub.mode: remote.")
    finally:
        probe.close()
    data_dir = ROOT / ((cfg.get("hub") or {}).get("local_data") or "hub_data")
    data_dir.mkdir(parents=True, exist_ok=True)
    import uvicorn
    app = create_app(data_dir, api_key=client.api_key or None, llm_defaults=_llm_defaults(cfg))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))
    threading.Thread(target=server.run, daemon=True, name="riko-hub").start()
    return server, port


def check(cfg):
    """Get to a hub that answers, or exit with a hint (what llm.check used to do)."""
    hub = cfg.get("hub") or {}
    mode = hub.get("mode") or "local"
    key_env = hub.get("api_key_env") or "RIKO_HUB_KEY"
    if mode not in ("local", "remote"):
        sys.exit(f"[setup] hub.mode: {mode} isn't a thing. Use local or remote.")
    client = connect(cfg)
    server, port = None, None
    if mode == "local":
        server, port = _start_local(cfg, client)
    elif not client.url:
        sys.exit("[setup] hub.mode: remote but hub.url is empty in character_config.yaml.")

    deadline = time.monotonic() + (20 if mode == "local" else 5)
    state, last = None, None
    while time.monotonic() < deadline:
        try:
            state = client.state()
            break
        except HubAuthError:
            sys.exit(f"[setup] The hub at {client.url} refused our key.\n"
                     f"        Put the hub's own key in .env as {key_env}=<the key> "
                     f"(same value as on the hub server).")
        except HubUnreachable as e:
            last = e
            if server is not None and getattr(server, "should_exit", False):
                break  # our own hub gave up; don't wait out the deadline
            time.sleep(0.2)
    if state is None:
        hint = ("Is it up? On the hub box: cd hub && docker compose up -d."
                if mode == "remote" else "The local hub never came up (see the uvicorn errors above).")
        sys.exit(f"[setup] {last}. {hint}")

    note_active(state)
    where = f"local on port {port}" if mode == "local" else client.url
    print(f"[setup] hub: {where}  |  persona: {state['active']['name']}"
          + (f"  |  model: {state.get('llm_model')}" if state.get("llm_model") else ""))
    return client


if __name__ == "__main__":
    # One test turn against whatever hub the config points at: uv run python -m riko.hub_client
    from riko.config import load_config
    cfg = load_config()
    hub = check(cfg)
    print("Riko:", hub.chat("Hi! Say something short.", source="terminal")["reply"])
