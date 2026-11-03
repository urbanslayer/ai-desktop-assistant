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

CHAT_TIMEOUT = 180     # reasoning models are slow
DEFAULT_TIMEOUT = 15
AVATAR_TIMEOUT = 120   # a .vrm can be tens of MB
AUDIO_TIMEOUT = 120    # the hub may still be synthesising when we ask


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

    def chat(self, text, source="desktop", persona=None):
        """One turn. Returns the ChatResult: {reply, persona, switched,
        message_ids, state_version}. A keyword turn (e.g. 'switch to asuka')
        comes back as switched=true with the persona's greeting as reply."""
        body = {"text": text, "source": source}
        if persona:
            body["persona"] = persona
        return self._json("POST", "/api/chat", json=body, timeout=CHAT_TIMEOUT)

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


def _reset():
    global _client, _active
    _client = None
    _active = None


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
    global _active
    _active = state.get("active") if isinstance(state, dict) else None


def active_persona():
    return _active


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
