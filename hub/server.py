"""The Riko hub server: personas, chat memory, settings and the web config page.

Two ways to run it, same code path:
  * local mode — `from hub.server import create_app` and mount in-process
  * standalone — `cd hub && python server.py` (what the Docker image does)

API contract: docs/DESIGN.md at the repo root; full reference in docs/API.md.
"""
import hmac
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # `python server.py` inside hub/ must find its siblings
    sys.path.insert(0, str(_HERE))

import threading

from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402

try:  # package import (repo root: `from hub.server import create_app`)
    from . import body as _body
    from . import brain as _brain
    from . import clients as _clients
    from . import emotion as _emotion
    from . import voice as _voice
    from .clients import ClientHub
    from .events import EventBus
    from .mcpmanager import (MCPManager, public_servers as public_mcp_servers,
                             sdk_available as mcp_available,
                             validate_servers as validate_mcp_servers)
    from .toolloop import run_turn
    from .tools.registry import build_hub_specs, hub_tool_info, permission_for, tools_prompt
    from .store import Store, default_voice, now_iso
except ImportError:  # standalone: modules sit next to this file (Docker: /app)
    import body as _body  # type: ignore[no-redef]
    import brain as _brain  # type: ignore[no-redef]
    import clients as _clients  # type: ignore[no-redef]
    import emotion as _emotion  # type: ignore[no-redef]
    import voice as _voice  # type: ignore[no-redef]
    from clients import ClientHub  # type: ignore[no-redef]
    from events import EventBus  # type: ignore[no-redef]
    from mcpmanager import (MCPManager,  # type: ignore[no-redef]
                            public_servers as public_mcp_servers,
                            sdk_available as mcp_available,
                            validate_servers as validate_mcp_servers)
    from toolloop import run_turn  # type: ignore[no-redef]
    from tools.registry import (build_hub_specs, hub_tool_info,  # type: ignore[no-redef]
                                permission_for, tools_prompt)
    from store import Store, default_voice, now_iso  # type: ignore[no-redef]

HUB_VERSION = "1"
log = logging.getLogger("riko.hub")
STATIC_DIR = _HERE / "static"
DEFAULT_AVATAR = _HERE / "assets" / "default_avatar.vrm"
MAX_AVATAR_BYTES = 200 * 1024 * 1024  # 200 MB
GLTF_MAGIC = b"glTF"

# Voice round (docs/VOICE.md)
MAX_REFERENCE_BYTES = 20 * 1024 * 1024          # reference clip upload cap
REFERENCE_MIN_SECONDS = 3.0                     # breeze wants a usable clip
REFERENCE_MAX_SECONDS = 30.0
MAX_TTS_TEXT = 1000                             # POST /api/tts text cap
MAX_AUDIO_CACHE_BYTES = 500 * 1024 * 1024       # data/audio total cap, oldest evicted
PREVIEW_LINE = "Hey senpai, this is how I sound right now. Not bad, right?"

# Breeze-TTS-2 settings defaults; the env overlay happens in main()/create_app.
# base_url "" means TTS is off (nothing to talk to); enabled is the user switch.
TTS_DEFAULTS = {"enabled": True, "base_url": "", "timeout": 120, "parallel_note": ""}

# Tools settings defaults (docs/ROUNDS.md R1-B, docs/TOOLS.md A). Blank
# searxng_url means unset: the default (RIKO_SEARXNG_URL) applies, same
# "blank = default" rule as the llm/tts blocks.
TOOLS_DEFAULTS = {
    "enabled": True,
    "max_steps": 8,           # model calls per turn (planning + final answer included)
    "max_seconds": 120,       # whole-turn wall budget for the tool loop
    "max_result_chars": 12000,  # per tool result before truncation
    "searxng_url": "",        # SearXNG base URL; empty = web_search unavailable
    "allow_private_fetch": False,  # let fetch_page hit loopback/private IPs (dev)
    "permissions": {},        # tool name -> always | ask | never
}

# MCP settings defaults (docs/ROUNDS.md R2-M): no servers out of the box.
MCP_DEFAULTS = {"servers": []}

# Seed persona: the system prompt copied from character_config.yaml at build
# time (the Docker image has no access to that file).
SEED_PERSONA = {
    "id": "riko",
    "name": "Riko",
    "keywords": ["riko"],
    "system_prompt": (
        "You are a helpful assistant named Riko.\n"
        "You speak like a snarky anime girl.\n"
        'Always refer to the user as "senpai".\n'
        "Keep replies short and conversational: one to three sentences, "
        "no markdown, no emoji."
    ),
    "greeting": "Riko here. Miss me, senpai?",
    "tts_voice": "",
}

# Seed persona's voice description (docs/VOICE.md): matches her character.
# The voice object itself (incl. the random seed) is built at seed time.
SEED_VOICE_DESCRIPTION = ("A playful, snarky young woman: quick, teasing and a "
                          "little smug, with a bright clear voice.")

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG.sub("-", str(name).strip().lower()).strip("-")


def normalize_switch_text(text: str) -> str:
    """Lower-case, trim, strip trailing . ! ? , — the form switch commands are matched in."""
    return str(text).strip().lower().rstrip(".!?,")


def create_app(data_dir, api_key: str | None = None,
               llm_defaults: dict | None = None,
               tts_defaults: dict | None = None,
               tools_defaults: dict | None = None) -> FastAPI:
    """Build the hub app over `data_dir` (SQLite + avatars live there).

    api_key None/empty disables auth. llm_defaults fills any LLM settings
    field the user has not saved via PUT /api/settings (api_key included);
    saved values always win. tts_defaults / tools_defaults do the same for
    the `tts` / `tools` settings objects (RIKO_TTS_* / RIKO_SEARXNG_URL)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    avatars_dir = data_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = data_dir / "audio"    # cached WAVs (docs/VOICE.md)
    audio_dir.mkdir(parents=True, exist_ok=True)
    voices_dir = data_dir / "voices"  # stored per-persona reference clips
    voices_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir / "hub.db")
    if not store.list_personas():  # first start on an empty data dir: seed Riko
        store.add_persona(pid=SEED_PERSONA["id"],
                          voice=default_voice(SEED_VOICE_DESCRIPTION),
                          **{k: v for k, v in SEED_PERSONA.items() if k != "id"})
        store.set_active(SEED_PERSONA["id"])
        store.set_version(1)

    brain = _brain.Brain()
    llm_defaults = dict(llm_defaults or {})
    tts_defaults = dict(tts_defaults or {})
    tools_defaults = dict(tools_defaults or {})
    app = FastAPI(title="riko-hub", version=HUB_VERSION, docs_url=None, redoc_url=None)
    chat_lock = threading.Lock()  # one chat at a time; concurrent requests queue
    events = EventBus()           # GET /api events ring buffer (R1-B)
    clients_hub = ClientHub(events)  # client-tool channel server side (R1-B)

    def bump_state() -> None:
        """Single choke point for state changes: version bump + event."""
        store.bump_version()
        events.publish("state_changed", version=store.version())

    # ── helpers ─────────────────────────────────────────────────────────
    # A base_url/model/reasoning_effort/api_key saved as "" used to SHADOW the
    # env default (the web page saving before its GET landed blanked the whole
    # LLM config). Blank means "unset" now: PUT deletes the key, and reads drop
    # legacy blanks left in databases written by the old page.
    _BLANK_MEANS_UNSET = ("base_url", "model", "reasoning_effort", "api_key")

    def stored_llm() -> dict:
        saved = dict(store.get_json("llm", {}))
        for key in _BLANK_MEANS_UNSET:
            if key in saved and not str(saved[key] or "").strip():
                del saved[key]
        return saved

    def default_llm() -> dict:
        merged = dict(_brain.LLM_DEFAULTS)
        merged.update(llm_defaults)
        return merged

    def llm_settings() -> dict:
        merged = default_llm()
        merged.update(stored_llm())
        return merged

    def stored_switch_phrases() -> list[str] | None:
        """Cleaned saved phrases, or None when unset/empty (empty = legacy
        leftover of the old page: there is no way to save 'no phrases')."""
        saved = store.get_json("switch_phrases", None)
        if saved is None:
            return None
        cleaned = [str(p).strip().lower() for p in saved if str(p).strip()]
        return cleaned or None

    def switch_phrases() -> list[str]:
        return stored_switch_phrases() or list(_brain.DEFAULT_SWITCH_PHRASES)

    # ── TTS (breeze) settings — same "blank = server default" semantics ──
    _TTS_BLANK_MEANS_UNSET = ("base_url", "parallel_note")

    def stored_tts() -> dict:
        saved = dict(store.get_json("tts", {}))
        for key in _TTS_BLANK_MEANS_UNSET:
            if key in saved and not str(saved[key] or "").strip():
                del saved[key]
        return saved

    def default_tts() -> dict:
        merged = dict(TTS_DEFAULTS)
        merged.update(tts_defaults)
        merged["enabled"] = bool(merged.get("enabled"))
        return merged

    def tts_settings() -> dict:
        merged = default_tts()
        merged.update(stored_tts())
        return merged

    def tts_on() -> bool:
        """TTS is on only with the user switch AND a base URL to talk to
        (RIKO_TTS_BASE_URL empty = TTS off, docs/VOICE.md)."""
        settings = tts_settings()
        return bool(settings.get("enabled")) and bool(str(settings.get("base_url") or "").strip())

    # ── tools settings — same "blank = server default" semantics ────────
    _TOOLS_BLANK_MEANS_UNSET = ("searxng_url",)

    def stored_tools() -> dict:
        saved = dict(store.get_json("tools", {}))
        for key in _TOOLS_BLANK_MEANS_UNSET:
            if key in saved and not str(saved[key] or "").strip():
                del saved[key]
        return saved

    def default_tools() -> dict:
        merged = dict(TOOLS_DEFAULTS)
        merged.update(tools_defaults)
        merged["enabled"] = bool(merged.get("enabled"))
        return merged

    def tools_settings() -> dict:
        merged = default_tools()
        merged.update(stored_tools())
        return merged

    def tools_status_response() -> dict:
        """GET /api/tools/status: whether the LLM server accepts the `tools`
        parameter at all (learned by trying; docs/ROUNDS.md fallback)."""
        st = store.get_json("tools_status", None) or {}
        return {"function_calling": st.get("function_calling", "unknown"),
                "reason": st.get("reason", "")}

    class ToolsStatus:  # called from the tool loop thread
        def note_tools_rejected(self, reason: str) -> None:
            if store.get_json("tools_status", {}).get("function_calling") != "rejected":
                store.set_json("tools_status", {"function_calling": "rejected",
                                                "reason": reason[:300]})

        def note_tools_worked(self) -> None:
            if store.get_json("tools_status", {}).get("function_calling") == "rejected":
                store.set_json("tools_status", {"function_calling": "ok", "reason": ""})

    tools_status = ToolsStatus()

    # ── MCP settings + manager (docs/ROUNDS.md R2-M) ───────────────────
    # settings `mcp.servers` has no env default: the list is exactly what
    # the user saved. The manager owns one asyncio loop thread for all
    # sessions; the hub boots fine without the mcp package (servers just
    # report state "unavailable").
    mcp_manager = MCPManager(events)

    def stored_mcp() -> dict:
        saved = store.get_json("mcp", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def mcp_servers() -> list[dict]:
        servers = stored_mcp().get("servers")
        return [s for s in servers if isinstance(s, dict)] \
            if isinstance(servers, list) else []

    def settings_response() -> dict:
        saved = stored_llm()
        defaults = default_llm()
        defaults_public = {k: v for k, v in defaults.items() if k != "api_key"}
        defaults_public["api_key_set"] = bool(defaults.get("api_key"))
        source = {k: ("saved" if k in saved else "default") for k in _brain.LLM_DEFAULTS}
        source["switch_phrases"] = "saved" if stored_switch_phrases() else "default"
        saved_tts = stored_tts()
        tts_source = {k: ("saved" if k in saved_tts else "default") for k in TTS_DEFAULTS}
        saved_tools = stored_tools()
        tools_source = {k: ("saved" if k in saved_tools else "default")
                        for k in TOOLS_DEFAULTS}
        has_mcp = isinstance(stored_mcp().get("servers"), list)
        return {
            "llm": public_llm(llm_settings()),
            "tts": tts_settings(),
            "tools": tools_settings(),
            "mcp": {"servers": public_mcp_servers(mcp_servers())},
            "switch_phrases": switch_phrases(),
            "defaults": {"llm": defaults_public,
                         "tts": default_tts(),
                         "tools": default_tools(),
                         "mcp": dict(MCP_DEFAULTS, servers=[]),
                         "switch_phrases": list(_brain.DEFAULT_SWITCH_PHRASES)},
            "llm_source": source,
            "tts_source": tts_source,
            "tools_source": tools_source,
            "mcp_source": {k: ("saved" if has_mcp else "default")
                           for k in MCP_DEFAULTS},
        }

    def persona_lookup() -> dict[str, str]:
        """lower(name/id/keyword) -> persona id (first match wins, in creation order)."""
        out: dict[str, str] = {}
        for p in store.list_personas():
            for key in [p["id"], p["name"].lower(), *p["keywords"]]:
                out.setdefault(str(key).strip().lower(), p["id"])
        return out

    def match_switch(norm: str) -> str | None:
        """Persona id if `norm` is a switch command, else None."""
        if not norm:
            return None
        table = persona_lookup()
        if norm in table:
            return table[norm]
        for phrase in switch_phrases():
            phrase = str(phrase).strip().lower()
            if not phrase:
                continue
            m = re.match(rf"^({re.escape(phrase)})\s+(.+)$", norm)
            if m:
                target = normalize_switch_text(m.group(2))
                if target in table:
                    return table[target]
        return None

    def state_object() -> dict:
        settings = llm_settings()
        active = store.get_persona(store.active()) or {}
        return {
            "version": store.version(),
            "active": public_persona(active),
            "personas": [{"id": p["id"], "name": p["name"]} for p in store.list_personas()],
            "last_message_id": store.last_message_id(),
            "llm_model": brain.cached_model(settings),
        }

    def public_llm(settings: dict) -> dict:
        out = {k: v for k, v in settings.items() if k != "api_key"}
        out["api_key_set"] = bool(settings.get("api_key"))
        return out

    def public_persona(persona: dict | None) -> dict | None:
        """Persona as the API shows it: voice.has_reference is read-only and
        derived from the stored clip on disk."""
        if not persona:
            return persona
        out = dict(persona)
        out["voice"] = dict(persona.get("voice") or {})
        out["voice"]["has_reference"] = reference_path(out["id"]).exists()
        return out

    def reference_path(persona_id: str) -> Path:
        return voices_dir / f"{persona_id}.wav"

    def err(status: int, detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status)

    # ── voice / audio plumbing (docs/VOICE.md) ─────────────────────────
    def voice_id_for(persona: dict) -> str:
        """breeze voice id for a clone persona; changes with voice_version so
        any edit forces a fresh registration/synthesis."""
        return f"riko-{persona['id']}-v{persona.get('voice_version', 1)}"

    def synth(persona: dict, text: str, emotion: str | None = None) -> bytes:
        """Blocking: one breeze utterance in this persona's voice. Returns
        proper WAV bytes. Raises _voice.TtsError subclasses (caller maps to
        HTTP); TtsNotFound on clone is recovered once by re-registering the
        stored clip (breeze may have been reset). `emotion` adds a delivery
        hint (docs/ROUNDS.md R1-B) — the emotion tag itself is never spoken."""
        breezy = _voice.Breeze(tts_settings())
        voice = dict(persona.get("voice") or {})
        mode = voice.get("mode", "design")
        style = str(voice.get("style") or "")
        hint = _emotion.tts_hint(emotion)
        if hint:
            style = (style + ", " + hint).strip(", ")
        seed = voice.get("seed")
        cfg = voice.get("cfg_scale")
        if mode == "clone":
            vid = voice_id_for(persona)
            try:
                return breezy.speech(text, voice=vid, instructions=style,
                                     seed=seed, cfg_scale=cfg)
            except _voice.TtsNotFound:
                clip = reference_path(persona["id"])
                if not clip.exists():
                    raise
                breezy.register_voice(vid, clip.read_bytes(),
                                      str(voice.get("ref_text") or ""))
                return breezy.speech(text, voice=vid, instructions=style,
                                     seed=seed, cfg_scale=cfg)
        # design (and 'off', for previews): describe the voice, fix the seed
        instructions = str(voice.get("description") or "")
        if style:
            instructions = (instructions + " " + style).strip()
        return breezy.speech(text, instructions=instructions, seed=seed, cfg_scale=cfg)

    def tts_gate(persona: dict | None) -> JSONResponse | None:
        """503/409 gate shared by every audio-producing route, or None to go on."""
        if not str(str_or_empty(tts_settings().get("base_url"))).strip():
            return err(503, "TTS not configured")
        if not tts_settings().get("enabled"):
            return err(503, "TTS disabled in settings")
        if persona is not None and (persona.get("voice") or {}).get("mode") == "off":
            return err(409, "this persona's voice is off")
        return None

    def str_or_empty(value) -> str:
        return "" if value is None else str(value)

    def tts_error_response(e: Exception) -> JSONResponse:
        if isinstance(e, _voice.TtsUnreachable):
            return err(503, str(e))
        if isinstance(e, _voice.TtsBusy):
            return err(503, str(e))
        return err(502, f"{type(e).__name__}: {e}")

    def wav_response(wav: bytes) -> Response:
        return Response(wav, media_type="audio/wav")

    def audio_cache_path(name: str) -> Path:
        return audio_dir / name

    def write_audio_cache(path: Path, wav: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(wav)
        tmp.replace(path)
        evict_audio_cache()

    def evict_audio_cache() -> None:
        """Keep data/audio under MAX_AUDIO_CACHE_BYTES, oldest files first."""
        try:
            files = sorted(audio_dir.glob("*.wav"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= MAX_AUDIO_CACHE_BYTES:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                total -= size
            except OSError:
                pass

    def delete_message_audio(message_ids: list[int]) -> None:
        for mid in message_ids:
            for f in audio_dir.glob(f"{mid}-v*.wav"):
                f.unlink(missing_ok=True)

    def delete_persona_audio(persona_id: str) -> None:
        for f in audio_dir.glob(f"greeting-{persona_id}-v*.wav"):
            f.unlink(missing_ok=True)

    def default_speech_text(persona: dict) -> str:
        return str(persona.get("greeting") or "").strip() or PREVIEW_LINE

    # ── auth ────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        path = request.url.path
        if api_key and path.startswith("/api/") and path != "/api/health":
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token, api_key):
                return err(401, "invalid api key")
        return await call_next(request)

    # ── routes ──────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": HUB_VERSION}

    @app.get("/api/state")
    def get_state():
        return state_object()

    @app.get("/api/personas")
    def get_personas():
        return [public_persona(p) for p in store.list_personas()]

    @app.post("/api/personas", status_code=201)
    async def post_persona(request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        name = str(body.get("name") or "").strip()
        if not name:
            return err(422, "name is required")
        pid = slugify(name)
        if not pid:
            return err(422, "name must contain letters or digits to make an id")
        if store.persona_exists(pid):
            return err(409, f"persona '{pid}' already exists")
        keywords = [str(k).strip().lower() for k in (body.get("keywords") or []) if str(k).strip()]
        if pid not in keywords:
            keywords.insert(0, pid)
        appearance = str(body.get("appearance") or "")
        if len(appearance) > _body.MAX_APPEARANCE_CHARS:
            return err(422, f"appearance is over {_body.MAX_APPEARANCE_CHARS} characters")
        persona = store.add_persona(
            pid, name, keywords,
            str(body.get("system_prompt") or ""),
            str(body.get("greeting") or ""),
            str(body.get("tts_voice") or ""),
            emotes=body["emotes"] if "emotes" in body else True,
            appearance=appearance,
            embodied=body["embodied"] if "embodied" in body else True,
        )
        bump_state()
        return JSONResponse(public_persona(persona), status_code=201)

    def persona_or_404(persona_id: str):
        return store.get_persona(persona_id)

    @app.get("/api/personas/{persona_id}")
    def get_persona(persona_id: str):
        persona = persona_or_404(persona_id)
        return public_persona(persona) or err(404, f"no persona '{persona_id}'")

    @app.put("/api/personas/{persona_id}")
    async def put_persona(persona_id: str, request: Request):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        fields: dict = {}
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not name:
                return err(422, "name must not be empty")
            fields["name"] = name
        for key in ("system_prompt", "greeting", "tts_voice"):
            if key in body:
                fields[key] = str(body[key] or "")
        if "emotes" in body:
            fields["emotes"] = bool(body["emotes"])
        if "appearance" in body:
            appearance = str(body["appearance"] or "")
            if len(appearance) > _body.MAX_APPEARANCE_CHARS:
                return err(422, f"appearance is over {_body.MAX_APPEARANCE_CHARS} characters")
            fields["appearance"] = appearance
        if "embodied" in body:
            fields["embodied"] = bool(body["embodied"])
        if "keywords" in body:
            if not isinstance(body["keywords"], list):
                return err(422, "keywords must be a list")
            fields["keywords"] = [str(k).strip().lower() for k in body["keywords"] if str(k).strip()]
        persona = store.update_persona(persona_id, fields) if fields else store.get_persona(persona_id)
        bump_state()
        return public_persona(persona)

    @app.delete("/api/personas/{persona_id}", status_code=204)
    def delete_persona(persona_id: str):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        remaining = [p["id"] for p in store.list_personas() if p["id"] != persona_id]
        if not remaining:
            return err(409, "cannot delete the last persona")
        was_active = store.active() == persona_id
        message_ids = store.message_ids(persona_id)
        store.delete_persona(persona_id)
        (avatars_dir / f"{persona_id}.vrm").unlink(missing_ok=True)
        reference_path(persona_id).unlink(missing_ok=True)
        delete_message_audio(message_ids)
        delete_persona_audio(persona_id)
        if was_active:
            store.set_active(remaining[0])
        bump_state()
        return Response(status_code=204)

    def avatar_etag(persona: dict) -> str:
        return f'"{persona["id"]}-{persona["avatar_version"]}"'

    @app.get("/api/personas/{persona_id}/avatar")
    def get_avatar(persona_id: str, request: Request):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        etag = avatar_etag(persona)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        path = avatars_dir / f"{persona_id}.vrm"
        if not path.exists():
            path = DEFAULT_AVATAR
        if not path.exists():
            return err(404, "no avatar on this hub")
        return FileResponse(path, media_type="model/gltf-binary", headers={"ETag": etag})

    @app.put("/api/personas/{persona_id}/avatar")
    async def put_avatar(persona_id: str, file: UploadFile = File(...)):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        data = await file.read(MAX_AVATAR_BYTES + 1)
        if len(data) > MAX_AVATAR_BYTES:
            return err(400, f"avatar is over {MAX_AVATAR_BYTES} bytes")
        if not data.startswith(GLTF_MAGIC):
            return err(400, "file is not a .vrm (missing glTF magic)")
        path = avatars_dir / f"{persona_id}.vrm"
        tmp = path.with_suffix(".vrm.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        persona = store.bump_avatar(persona_id)
        bump_state()
        return public_persona(persona)

    @app.post("/api/active")
    async def post_active(request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        raw_wanted = body.get("persona")
        wanted = normalize_switch_text(str(raw_wanted or ""))
        if not wanted:
            return err(400, "persona is required")
        target = persona_lookup().get(wanted)
        if not target:
            return err(404, f"no persona matches '{raw_wanted}'")
        if store.set_active(target):
            bump_state()
        return state_object()

    def history_persona(request: Request) -> str | None:
        pid = request.query_params.get("persona") or store.active()
        return pid if store.persona_exists(pid) else None

    @app.get("/api/history")
    def get_history(request: Request):
        pid = history_persona(request)
        if not pid:
            return err(404, "no such persona")
        try:
            after = max(0, int(request.query_params.get("after") or 0))
            limit = min(500, max(1, int(request.query_params.get("limit") or 50)))
        except ValueError:
            return err(400, "after and limit must be integers")
        return {"persona": pid, "messages": store.messages_after(pid, after, limit)}

    @app.delete("/api/history", status_code=204)
    def delete_history(request: Request):
        pid = history_persona(request)
        if not pid:
            return err(404, "no such persona")
        delete_message_audio(store.message_ids(pid))  # cached audio goes too
        store.clear_messages(pid)
        bump_state()
        return Response(status_code=204)

    @app.post("/api/history/import")
    async def post_import(request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        pid = str((body or {}).get("persona") or "") or store.active()
        if not store.persona_exists(pid):
            return err(404, f"no persona '{pid}'")
        messages = body.get("messages")
        if not isinstance(messages, list):
            return err(422, "messages must be a list")
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") \
                    or not isinstance(m.get("content"), str):
                return err(422, 'each message needs role ("user"/"assistant") and content')
        if body.get("replace"):
            store.clear_messages(pid)
        for m in messages:
            store.add_message(pid, m["role"], m["content"], "import")
        if messages:
            bump_state()
        return {"imported": len(messages)}

    @app.post("/api/chat")
    async def post_chat(request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        text = str(body.get("text") or "").strip()
        if not text:
            return err(422, "text is required")
        source = str(body.get("source") or "")
        override = str(body.get("persona") or "")
        speak = body.get("speak", True) is not False  # default true (docs/VOICE.md)
        norm = normalize_switch_text(text)

        def turn():  # blocking section: store + LLM, one chat at a time
            with chat_lock:
                target = match_switch(norm)
                if target:  # switch: activate, greet, store nothing
                    if store.set_active(target):
                        bump_state()
                    persona = store.get_persona(target) or {}
                    reply = persona.get("greeting") or f"{persona.get('name', target)} here."
                    # docs/BODY.md: a switch greets with a wave when the
                    # online body set can do it and the persona is embodied.
                    greet_actions = (
                        [{"name": "wave", "at": 0.0}]
                        if persona.get("embodied", True)
                        and "wave" in clients_hub.body_actions()
                        else [])
                    audio_url = None
                    if speak and tts_on() and (persona.get("voice") or {}).get("mode") != "off":
                        audio_url = (f"/api/personas/{target}/greeting/audio"
                                     f"?v={persona.get('voice_version', 1)}")
                    events.publish("turn_started", persona=target, source=source)
                    events.publish("turn_finished", persona=target,
                                   message_id=None, emotion=None,
                                   actions=greet_actions)
                    return ("ok", {"reply": reply, "persona": target, "switched": True,
                                   "emotion": None, "tools_used": [], "actions": greet_actions,
                                   "message_ids": [], "state_version": store.version(),
                                   "audio_url": audio_url})

                pid = override or store.active()
                persona = store.get_persona(pid)
                if not persona:
                    return ("err", 404, f"no persona '{pid}'")
                settings = llm_settings()
                if not settings.get("base_url"):
                    return ("err", 503, "LLM not configured")

                # offered tools: hub built-ins + online client tools, minus
                # anything the permission table says `never` (docs/ROUNDS.md)
                tools_cfg = tools_settings()
                specs: list = []
                if tools_cfg.get("enabled"):
                    permissions = tools_cfg.get("permissions") or {}
                    for s in build_hub_specs(tools_cfg):
                        if permission_for(s.name, "hub", s.risk, permissions) != "never":
                            specs.append(s)
                    for s in clients_hub.online_tool_specs():
                        if permission_for(s.name, "client", s.risk, permissions) != "never":
                            specs.append(s)
                    # MCP tools (R2-M): mcp_<server>__<tool>, the manager's
                    # per-server tool_permissions override beats the map
                    for s in mcp_manager.tool_specs():
                        if permission_for(s.name, "mcp", s.risk, permissions,
                                          s.permission_override) != "never":
                            specs.append(s)
                offered_names = [s.name for s in specs if s.available]

                system = persona["system_prompt"]
                if persona.get("emotes"):
                    system += "\n\n" + _emotion.PROMPT_RULE
                # docs/BODY.md: the body block only when an embodied client
                # is online AND the persona is embodied; the appearance line
                # always (text-only chats can describe her too).
                body_actions = clients_hub.body_actions()
                if persona.get("embodied", True) and body_actions:
                    system += "\n\n" + _body.body_prompt(body_actions)
                    # one-line nudge when this very message asks for a
                    # gesture: cheap and raises tag compliance
                    asked_for = _body.detect_action_request(text, body_actions)
                    if asked_for:
                        system += "\n\n" + _body.request_hint(asked_for)
                appearance = str(persona.get("appearance") or "").strip()
                if appearance:
                    system += "\n\n" + _body.appearance_prompt(appearance)
                if offered_names:
                    system += "\n\n" + tools_prompt(offered_names)

                max_history = int(settings.get("max_history") or 40)
                history = store.recent_messages(pid, max_history)
                messages = ([{"role": "system", "content": system}]
                            + [{"role": m["role"], "content": m["content"]} for m in history]
                            + [{"role": "user", "content": text}])

                events.publish("turn_started", persona=pid, source=source)
                ctx = {"persona": pid, "events": events, "log": store.log_tool,
                       "status": tools_status, "clients": clients_hub,
                       "deadline": time.monotonic()
                                   + float(tools_cfg.get("max_seconds") or 120)}
                try:
                    out = run_turn(brain, settings, tools_cfg, specs, messages, ctx)
                except Exception as e:
                    return ("err", 502, f"{type(e).__name__}: {e}")

                # the emotion tag never reaches the stored reply or the TTS
                emotion, answer = _emotion.parse_emotion(out["text"])
                # body actions are parsed from the FINAL reply only (the
                # tool loop may have streamed several model turns)
                answer, actions, ignored = _body.parse_actions(answer, body_actions)
                if ignored:
                    log.info("ignored_actions persona=%s names=%s", pid, ",".join(ignored))
                # docs/BODY.md §3 asked-for fallback: tag emission is
                # stochastic (worse at high temperature), so an explicit
                # request the reply forgot is added anyway; the model's own
                # matching tag is just flagged. Only for embodied personas
                # with a body that can play it (empty set otherwise).
                actions = _body.apply_asked(
                    text, actions, body_actions if persona.get("embodied", True) else [])
                tools_used = out["tools_used"]

                user_id = store.add_message(pid, "user", text, source)
                assistant_id = store.add_message(pid, "assistant", answer, source,
                                                 emotion=emotion, tools=tools_used,
                                                 actions=actions)
                bump_state()
                events.publish("turn_finished", persona=pid,
                               message_id=assistant_id, emotion=emotion,
                               actions=actions)
                # The chat never waits for audio: audio_url points at the audio
                # route, which synthesises lazily (and caches on disk).
                audio_url = None
                if speak and tts_on() and (persona.get("voice") or {}).get("mode") != "off":
                    audio_url = f"/api/messages/{assistant_id}/audio"
                return ("ok", {"reply": answer, "persona": pid, "switched": False,
                               "emotion": emotion, "tools_used": tools_used,
                               "actions": actions,
                               "message_ids": [user_id, assistant_id],
                               "state_version": store.version(),
                               "audio_url": audio_url})

        kind, *rest = await run_in_threadpool(turn)
        return rest[0] if kind == "ok" else err(rest[0], rest[1])

    @app.get("/api/settings")
    def get_settings():
        return settings_response()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        saved_llm = stored_llm()
        llm = body.get("llm")
        if llm is not None:
            if not isinstance(llm, dict):
                return err(422, "llm must be an object")
            # blank base_url / model / reasoning_effort = "unset this override,
            # fall back to the default" (model's default is RIKO_LLM_MODEL or 'auto')
            for key in ("base_url", "model", "reasoning_effort"):
                if key in llm:
                    value = str(llm[key] or "").strip()
                    if value:
                        saved_llm[key] = value
                    else:
                        saved_llm.pop(key, None)
            # api_key is write-only and NEVER blanked by accident: an empty/absent
            # api_key leaves the stored key alone; only clear_api_key: true removes
            # it (then the env default applies — no way to force a keyless LLM).
            if "api_key" in llm:
                value = str(llm["api_key"] or "")
                if value.strip():
                    saved_llm["api_key"] = value
            if llm.get("clear_api_key"):
                saved_llm.pop("api_key", None)
            for key, lo in (("temperature", 0.0), ("max_tokens", 1), ("max_history", 1)):
                if key in llm:
                    try:
                        num = float(llm[key])
                    except (TypeError, ValueError):
                        return err(422, f"{key} must be a number")
                    if num < lo:
                        return err(422, f"{key} must be >= {lo}")
                    saved_llm[key] = int(num) if key != "temperature" else num
            if "extra_body" in llm:
                if not isinstance(llm["extra_body"], dict):
                    return err(422, "extra_body must be a JSON object")
                saved_llm["extra_body"] = llm["extra_body"]
            store.set_json("llm", saved_llm)
            brain.invalidate()  # settings changed: re-resolve model 'auto'
        if "switch_phrases" in body:
            phrases = body["switch_phrases"]
            if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
                return err(422, "switch_phrases must be a list of strings")
            cleaned = [p.strip().lower() for p in phrases if p.strip()]
            # [] means "reset", not "no phrases": keywords/names always switch
            if cleaned:
                store.set_json("switch_phrases", cleaned)
            else:
                store.del_json("switch_phrases")
        if "tts" in body:
            tts = body["tts"]
            if not isinstance(tts, dict):
                return err(422, "tts must be an object")
            saved_tts = stored_tts()
            # blank base_url / parallel_note = "unset the override, back to
            # the RIKO_TTS_* default" — same rule as the llm block.
            for key in ("base_url", "parallel_note"):
                if key in tts:
                    value = str(tts[key] or "").strip()
                    if value:
                        saved_tts[key] = value
                    else:
                        saved_tts.pop(key, None)
            if "enabled" in tts:
                if not isinstance(tts["enabled"], bool):
                    return err(422, "tts.enabled must be a boolean")
                saved_tts["enabled"] = tts["enabled"]
            if "timeout" in tts:
                try:
                    num = float(tts["timeout"])
                except (TypeError, ValueError):
                    return err(422, "tts.timeout must be a number")
                if num < 1:
                    return err(422, "tts.timeout must be >= 1")
                saved_tts["timeout"] = int(num)
            store.set_json("tts", saved_tts)
        if "tools" in body:
            tools = body["tools"]
            if not isinstance(tools, dict):
                return err(422, "tools must be an object")
            saved_tools = stored_tools()
            # blank searxng_url = "unset the override, back to the
            # RIKO_SEARXNG_URL default" — same rule as the llm/tts blocks.
            for key in _TOOLS_BLANK_MEANS_UNSET:
                if key in tools:
                    value = str(tools[key] or "").strip()
                    if value:
                        saved_tools[key] = value
                    else:
                        saved_tools.pop(key, None)
            for key in ("enabled", "allow_private_fetch"):
                if key in tools:
                    if not isinstance(tools[key], bool):
                        return err(422, f"tools.{key} must be a boolean")
                    saved_tools[key] = tools[key]
            for key, lo, hi in (("max_steps", 1, 32), ("max_seconds", 5, 600),
                                ("max_result_chars", 500, 100000)):
                if key in tools:
                    try:
                        num = int(tools[key])
                    except (TypeError, ValueError):
                        return err(422, f"tools.{key} must be an integer")
                    if not lo <= num <= hi:
                        return err(422, f"tools.{key} must be between {lo} and {hi}")
                    saved_tools[key] = num
            if "permissions" in tools:
                perms = tools["permissions"]
                if not isinstance(perms, dict):
                    return err(422, "tools.permissions must be an object")
                cleaned_perms: dict[str, str] = {}
                for name, perm in perms.items():
                    if perm is None:  # null removes the explicit override
                        continue      # -> back to the risk-based default
                    if str(perm) not in ("always", "ask", "never"):
                        return err(422, f"permission for '{name}' must be "
                                        "always, ask, never or null")
                    cleaned_perms[str(name)[:80]] = str(perm)
                saved_tools["permissions"] = cleaned_perms
            store.set_json("tools", saved_tools)
        if "mcp" in body:
            mcp = body["mcp"]
            if not isinstance(mcp, dict):
                return err(422, "mcp must be an object")
            if "servers" in mcp:
                saved_mcp = stored_mcp()
                try:
                    cleaned_servers = validate_mcp_servers(
                        mcp["servers"], saved_mcp.get("servers") or [])
                except ValueError as e:
                    return err(422, str(e))
                saved_mcp["servers"] = cleaned_servers
                store.set_json("mcp", saved_mcp)
                try:
                    mcp_manager.sync(cleaned_servers)
                except Exception as e:  # never fail the save on the reconnect
                    events.publish("mcp_error", error=f"{type(e).__name__}: {e}"[:300])
        bump_state()
        return settings_response()

    @app.post("/api/settings/reset")
    async def post_settings_reset(request: Request):
        """Drop saved overrides so the defaults (built-in + RIKO_LLM_* env) apply:
        {"llm": true, "switch_phrases": true} — either side optional."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        changed = False
        if body.get("llm"):
            changed |= store.del_json("llm")
            brain.invalidate()
        if body.get("tts"):
            changed |= store.del_json("tts")
        if body.get("tools"):
            changed |= store.del_json("tools")
        if body.get("mcp"):
            changed |= store.del_json("mcp")
            mcp_manager.sync(mcp_servers())
        if body.get("switch_phrases"):
            changed |= store.del_json("switch_phrases")
        if not (body.get("llm") or body.get("tts") or body.get("tools")
                or body.get("mcp") or body.get("switch_phrases")):
            return err(422, 'set "llm", "tts", "tools", "mcp" and/or "switch_phrases" to true')
        if changed:
            bump_state()
        return settings_response()

    @app.post("/api/settings/test")
    def post_settings_test():
        """Validate the merged settings cheaply: one GET /v1/models against the
        LLM server + one GET /health against breeze-tts. Never reveals keys."""
        out: dict = {}
        settings = llm_settings()
        try:
            out.update({"ok": True, "model": brain.test(settings)})
        except Exception as e:
            out.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        tts = tts_settings()
        if not str(tts.get("base_url") or "").strip():
            out["tts"] = {"ok": False, "error": "TTS not configured"}
        elif not tts.get("enabled"):
            out["tts"] = {"ok": False, "error": "TTS disabled in settings"}
        else:
            try:
                _voice.Breeze(tts).health()
                out["tts"] = {"ok": True}
            except Exception as e:
                out["tts"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return out

    # ── voice: audio routes (docs/VOICE.md) ────────────────────────────
    def serve_cached_audio(name: str, persona: dict, text: str,
                           emotion: str | None = None):
        """Shared body of the two cached audio routes: cached WAV if we have
        it, else one blocking breeze synthesis (made once, then cached).
        The emotion is part of the cache key (different delivery per mood)."""
        blocked = tts_gate(persona)
        if blocked is not None:
            return blocked
        path = audio_cache_path(name)
        if not path.exists():
            try:
                wav = synth(persona, text, emotion)
            except Exception as e:
                return tts_error_response(e)
            write_audio_cache(path, wav)
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/messages/{message_id}/audio")
    def get_message_audio(message_id: int):
        message = store.get_message(message_id)
        if not message or message["role"] != "assistant":
            return err(404, "no assistant message with that id")
        persona = store.get_persona(message["persona"])
        if not persona:
            return err(404, "persona of that message no longer exists")
        emotion = message.get("emotion")
        # 'neutral' sounds like no hint at all: same cache file, no duplicate WAV
        suffix = "" if not emotion or emotion == "neutral" else f"-{emotion}"
        return serve_cached_audio(f"{message_id}-v{persona['voice_version']}{suffix}.wav",
                                  persona, message["content"],
                                  None if not emotion or emotion == "neutral" else emotion)

    @app.get("/api/personas/{persona_id}/greeting/audio")
    def get_greeting_audio(persona_id: str, request: Request):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        # ?v= is informational (clients cache by it); the on-disk key is server-side
        return serve_cached_audio(
            f"greeting-{persona_id}-v{persona['voice_version']}.wav",
            persona, default_speech_text(persona))

    @app.post("/api/tts")
    async def post_tts(request: Request):
        """One-shot WAV for previews and client lines; never cached."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        text = str(body.get("text") or "").strip()
        if not text:
            return err(422, "text is required")
        if len(text) > MAX_TTS_TEXT:
            return err(422, f"text is over {MAX_TTS_TEXT} characters")
        pid = str(body.get("persona") or "") or store.active()
        persona = store.get_persona(pid)
        if not persona:
            return err(404, f"no persona '{pid}'")
        emotion = str(body.get("emotion") or "") or None
        if emotion is not None and emotion not in _emotion.EMOTIONS:
            return err(422, "emotion must be one of " + ", ".join(_emotion.EMOTIONS))
        blocked = tts_gate(persona)
        if blocked is not None:
            return blocked
        try:
            wav = await run_in_threadpool(synth, persona, text, emotion)
        except Exception as e:
            return tts_error_response(e)
        return wav_response(wav)

    @app.get("/api/tts/status")
    def get_tts_status():
        settings = tts_settings()
        enabled = tts_on()
        reachable, voices = False, 0
        if enabled:
            breezy = _voice.Breeze(settings)
            try:
                breezy.health()
                reachable = True
                voices = len(breezy.voices())
            except Exception:
                pass
        return {"enabled": enabled, "reachable": reachable,
                "base_url": str(settings.get("base_url") or ""), "voices": voices}

    # ── voice: persona voice editing ───────────────────────────────────
    def validate_voice_fields(raw: dict) -> tuple[dict, str | None]:
        """PUT /voice rules: check a raw voice-field dict and return
        (cleaned fields, error message). Nothing here touches the store —
        the preview override runs through the exact same validation."""
        for key in raw:
            if key not in ("mode", "description", "style", "ref_text", "seed", "cfg_scale"):
                return {}, f"unknown voice field '{key}'"
        out: dict = {}
        if "mode" in raw:
            mode = str(raw["mode"] or "")
            if mode not in ("design", "clone", "off"):
                return {}, "voice.mode must be design, clone or off"
            out["mode"] = mode
        for key in ("description", "style", "ref_text"):
            if key in raw:
                out[key] = str(raw[key] or "")
        if "seed" in raw:
            if isinstance(raw["seed"], bool):
                return {}, "voice.seed must be an integer"
            try:
                seed = int(raw["seed"])
            except (TypeError, ValueError):
                return {}, "voice.seed must be an integer"
            if seed < 0:
                return {}, "voice.seed must be a non-negative integer"
            out["seed"] = seed
        if "cfg_scale" in raw:
            if raw["cfg_scale"] is None:
                out["cfg_scale"] = None
            else:
                try:
                    cfg = float(raw["cfg_scale"])
                except (TypeError, ValueError):
                    return {}, "voice.cfg_scale must be a number or null"
                if not (0 < cfg <= 10):
                    return {}, "voice.cfg_scale must be in (0, 10] or null"
                out["cfg_scale"] = cfg
        return out, None

    @app.put("/api/personas/{persona_id}/voice")
    async def put_persona_voice(persona_id: str, request: Request):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        cleaned, bad = validate_voice_fields(body)
        if bad is not None:
            return err(422, bad)
        if cleaned.get("mode") == "clone" and not reference_path(persona_id).exists():
            return err(422, "no reference clip on this persona — upload one first")
        voice = dict(persona.get("voice") or {})
        voice.update(cleaned)
        voice.pop("has_reference", None)  # read-only, derived from disk
        changed = voice != {k: v for k, v in (persona.get("voice") or {}).items()
                            if k != "has_reference"}
        if changed:
            persona = store.set_voice(persona_id, voice)
            bump_state()
        return public_persona(persona)

    @app.put("/api/personas/{persona_id}/voice/reference")
    async def put_voice_reference(persona_id: str, file: UploadFile = File(...),
                                  ref_text: str = Form("")):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        ref_text = str(ref_text or "").strip()
        if not ref_text:
            return err(400, "ref_text (the exact transcript of the clip) is required")
        raw = await file.read(MAX_REFERENCE_BYTES + 1)
        if len(raw) > MAX_REFERENCE_BYTES:
            return err(400, f"reference clip is over {MAX_REFERENCE_BYTES} bytes")
        try:
            data, sample_rate = _voice.decode_upload(raw)
        except ValueError as e:
            return err(400, str(e))
        seconds = len(data) / sample_rate
        if not (REFERENCE_MIN_SECONDS <= seconds <= REFERENCE_MAX_SECONDS):
            return err(400, f"reference clip must be {REFERENCE_MIN_SECONDS:.0f}"
                            f"-{REFERENCE_MAX_SECONDS:.0f} seconds, got {seconds:.1f}s")
        wav = _voice.encode_wav(data, sample_rate)  # re-encoded mono WAV
        # breeze must accept the clip BEFORE anything changes (a failed
        # upload leaves the persona exactly as it was).
        breezy = _voice.Breeze(tts_settings())
        if not str(tts_settings().get("base_url") or "").strip():
            return err(503, "TTS not configured — cannot register a reference clip")
        if not tts_settings().get("enabled"):
            return err(503, "TTS disabled in settings — cannot register a reference clip")
        new_version = int(persona.get("voice_version", 1)) + 1
        try:
            breezy.register_voice(f"riko-{persona_id}-v{new_version}", wav, ref_text)
        except Exception as e:
            return tts_error_response(e)
        path = reference_path(persona_id)
        tmp = path.with_suffix(".wav.tmp")
        tmp.write_bytes(wav)
        tmp.replace(path)
        voice = dict(persona.get("voice") or {})
        voice["mode"] = "clone"
        voice["ref_text"] = ref_text
        persona = store.set_voice(persona_id, voice)
        bump_state()
        return public_persona(persona)

    @app.get("/api/personas/{persona_id}/voice/reference")
    def get_voice_reference(persona_id: str):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        path = reference_path(persona_id)
        if not path.exists():
            return err(404, "no reference clip on this persona")
        return FileResponse(path, media_type="audio/wav")

    @app.delete("/api/personas/{persona_id}/voice/reference")
    def delete_voice_reference(persona_id: str):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        if not reference_path(persona_id).exists():
            return err(404, "no reference clip on this persona")
        reference_path(persona_id).unlink(missing_ok=True)
        voice = dict(persona.get("voice") or {})
        voice["mode"] = "design"  # back to a designed voice (docs/VOICE.md)
        persona = store.set_voice(persona_id, voice)
        bump_state()
        return public_persona(persona)

    @app.post("/api/personas/{persona_id}/voice/preview")
    async def post_voice_preview(persona_id: str, request: Request):
        """One uncached WAV in this persona's voice — the page's Preview.
        Body: {"text"?: str, "voice"?: {mode, description, style, ref_text,
        seed, cfg_scale}}. The `voice` override lets the page audition the
        values in its panel BEFORE saving: validated exactly like PUT
        /voice, applied to the stored voice for this one synthesis only —
        nothing is stored and nothing is cached. Works even with mode off,
        so you can audition before enabling."""
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        try:
            body = await request.body()
            body = json.loads(body) if body.strip() else {}
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        text = str(body.get("text") or "").strip() or default_speech_text(persona)
        voice = {k: v for k, v in (persona.get("voice") or {}).items()
                 if k != "has_reference"}
        override = body.get("voice")
        if override is not None:
            if not isinstance(override, dict):
                return err(422, "voice override must be an object")
            cleaned, bad = validate_voice_fields(override)
            if bad is not None:
                return err(422, bad)
            voice.update(cleaned)
        if voice.get("mode", "design") == "clone" and not reference_path(persona_id).exists():
            return err(422, "no reference clip on this persona — upload one first "
                             "(or preview in design mode)")
        blocked = tts_gate(None)
        if blocked is not None:
            return blocked
        try:
            wav = await run_in_threadpool(synth, {**persona, "voice": voice}, text)
        except Exception as e:
            return tts_error_response(e)
        return wav_response(wav)

    @app.post("/api/personas/{persona_id}/voice/lock")
    async def post_voice_lock(persona_id: str, request: Request):
        """Freeze a designed voice: say `text` in it, store that clip as the
        reference, register it, switch the persona to clone mode."""
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        try:
            body = await request.body()
            body = json.loads(body) if body.strip() else {}
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        mode = (persona.get("voice") or {}).get("mode")
        if mode != "design":
            return err(409, f"only a designed voice can be locked (mode is '{mode}')")
        text = str(body.get("text") or "").strip() or default_speech_text(persona)
        blocked = tts_gate(None)
        if blocked is not None:
            return blocked
        try:
            breezy = _voice.Breeze(tts_settings())
            wav = await run_in_threadpool(synth, persona, text)
            new_version = int(persona.get("voice_version", 1)) + 1
            await run_in_threadpool(
                breezy.register_voice, f"riko-{persona_id}-v{new_version}", wav, text)
        except Exception as e:
            return tts_error_response(e)  # nothing changed
        path = reference_path(persona_id)
        tmp = path.with_suffix(".wav.tmp")
        tmp.write_bytes(wav)
        tmp.replace(path)
        voice = dict(persona.get("voice") or {})
        voice["mode"] = "clone"
        voice["ref_text"] = text
        persona = store.set_voice(persona_id, voice)
        bump_state()
        return public_persona(persona)

    # ── events (docs/ROUNDS.md R1-B) ───────────────────────────────────
    @app.get("/api/events")
    async def get_events(request: Request):
        """Long-poll the event ring buffer: ?after=<last seq>&wait=<seconds>.
        Returns {events: [...], last}: pass `last` back as the next `after`.
        Event types: turn_started, tool_started, tool_finished,
        approval_needed, turn_finished, state_changed."""
        qp = request.query_params
        try:
            after = int(qp.get("after") or 0)
            wait = float(qp.get("wait") or 25)
        except ValueError:
            return err(422, "after must be an integer and wait a number")
        out = await run_in_threadpool(events.wait, after, wait)
        return out

    # ── tools (docs/ROUNDS.md R1-B, docs/TOOLS.md) ─────────────────────
    @app.get("/api/tools")
    def get_tools():
        """Everything the Tools section of the page (and the desktop) shows:
        the built-ins plus every ONLINE client tool, each with its effective
        permission, plus the merged settings and the function-calling status."""
        cfg = tools_settings()
        permissions = cfg.get("permissions") or {}
        tools = hub_tool_info(cfg, permissions)
        for s in clients_hub.online_tool_specs():
            tools.append({"name": s.name, "display": s.display, "source": "client",
                          "risk": s.risk, "available": True,
                          "description": s.description,
                          "permission": permission_for(s.name, "client", s.risk,
                                                       permissions),
                          "client_id": s.client_id})
        for s in mcp_manager.tool_info():
            tools.append({**s,
                          "permission": permission_for(s["name"], "mcp", s["risk"],
                                                       permissions,
                                                       s["permission_override"])})
        return {"tools": tools, "settings": cfg, "defaults": default_tools(),
                "status": tools_status_response(),
                # online body set (docs/BODY.md §1): what she can do right now
                "body_actions": clients_hub.body_actions()}

    @app.get("/api/tools/log")
    def get_tools_log(request: Request):
        try:
            limit = int(request.query_params.get("limit") or 50)
        except ValueError:
            return err(422, "limit must be an integer")
        return {"entries": store.tool_log(limit)}

    @app.get("/api/tools/status")
    def get_tools_status():
        return tools_status_response()

    # ── MCP plug-ins (docs/ROUNDS.md R2-M, docs/API.md) ────────────────
    @app.get("/api/mcp/status")
    def get_mcp_status():
        """One row per configured server: {name, transport, state
        (starting|ready|error|disabled|unavailable), error, tools, last_ok}."""
        return mcp_manager.status(mcp_servers())

    @app.post("/api/mcp/{name}/restart")
    def post_mcp_restart(name: str):
        """Reconnect a server now (skips the reconnect backoff)."""
        outcome = mcp_manager.restart(name)
        if outcome == "unknown":
            return err(404, f"no MCP server '{name}'")
        if outcome == "disabled":
            return err(409, f"MCP server '{name}' is disabled")
        return {"ok": True}

    # ── client-tool channel (docs/ROUNDS.md R1-B) ──────────────────────
    @app.get("/api/clients")
    def get_clients():
        """Online clients and the tools they can run right now."""
        return {"clients": clients_hub.online_clients(),
                "heartbeat_s": _clients.HEARTBEAT_S,
                "offline_after_s": _clients.OFFLINE_AFTER_S}

    @app.post("/api/clients/{client_id}/register")
    async def post_client_register(client_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        try:
            out = clients_hub.register(client_id, str(body.get("name") or ""),
                                       str(body.get("kind") or ""),
                                       body.get("tools") or [],
                                       body=body.get("body"))
        except ValueError as e:
            return err(422, str(e))
        return out

    @app.post("/api/clients/{client_id}/heartbeat")
    def post_client_heartbeat(client_id: str):
        if not clients_hub.heartbeat(client_id):
            return err(404, "register first")
        return {"ok": True, "heartbeat_s": _clients.HEARTBEAT_S}

    @app.get("/api/clients/{client_id}/requests")
    async def get_client_requests(client_id: str, request: Request):
        """Long-poll for tool calls to run (clients poll every ~30 s cycle;
        `wait` is capped at 60 s). An empty list means 'nothing yet'."""
        try:
            wait = float(request.query_params.get("wait") or 25)
        except ValueError:
            return err(422, "wait must be a number")
        try:
            reqs = await run_in_threadpool(clients_hub.take_requests, client_id, wait)
        except KeyError:
            return err(404, "register first")
        return {"requests": reqs,
                "approval_timeout_s": _clients.APPROVAL_TIMEOUT_S}

    @app.post("/api/clients/{client_id}/results")
    async def post_client_result(client_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        out = clients_hub.submit_result(
            client_id, str(body.get("request_id") or ""),
            bool(body.get("ok")), body.get("result"), body.get("error"),
            body.get("approved"))
        if out is None:
            return err(404, "unknown request (already timed out?)")
        return {"ok": True, **out}

    @app.delete("/api/clients/{client_id}", status_code=204)
    def delete_client(client_id: str):
        clients_hub.forget(client_id)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


    # ── standalone ──────────────────────────────────────────────────────
    app.state.store = store
    app.state.brain = brain
    app.state.mcp = mcp_manager

    # MCP servers from a previous run connect in the background as soon as
    # the app exists (works in local mode too, where the parent app may not
    # forward lifespan events); clean shutdown closes the sessions.
    try:
        mcp_manager.sync(mcp_servers())
    except Exception as e:
        events.publish("mcp_error", error=f"startup sync failed: "
                                          f"{type(e).__name__}: {e}"[:300])

    @app.on_event("shutdown")
    async def _mcp_shutdown():
        await run_in_threadpool(mcp_manager.shutdown)

    return app


def main():
    import uvicorn

    data_dir = os.getenv("RIKO_HUB_DATA") or str(Path.cwd() / "data")
    llm_defaults = {
        "base_url": os.getenv("RIKO_LLM_BASE_URL", ""),
        "api_key": os.getenv("RIKO_LLM_API_KEY", ""),
        "model": os.getenv("RIKO_LLM_MODEL", "auto"),
    }
    tts_defaults = {
        "base_url": os.getenv("RIKO_TTS_BASE_URL", ""),  # empty = TTS off
        "enabled": os.getenv("RIKO_TTS_ENABLED", "1").strip().lower()
                   in ("1", "true", "yes", "on"),
    }
    tools_defaults = {
        "searxng_url": os.getenv("RIKO_SEARXNG_URL", ""),  # empty = web_search off
        "allow_private_fetch": os.getenv("RIKO_ALLOW_PRIVATE_FETCH", "")
                               .strip().lower() in ("1", "true", "yes", "on"),
    }
    app = create_app(data_dir, api_key=os.getenv("RIKO_HUB_KEY") or None,
                     llm_defaults=llm_defaults, tts_defaults=tts_defaults,
                     tools_defaults=tools_defaults)
    host = os.getenv("RIKO_HUB_HOST", "0.0.0.0")
    port = int(os.getenv("RIKO_HUB_PORT", "8002"))
    print(f"[hub] data={data_dir} llm={llm_defaults['base_url'] or '(not configured)'} "
          f"tts={tts_defaults['base_url'] or '(off)'} "
          f"searxng={tools_defaults['searxng_url'] or '(off)'} "
          f"mcp={'on' if mcp_available() else 'unavailable: mcp package not installed'}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
