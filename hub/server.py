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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # `python server.py` inside hub/ must find its siblings
    sys.path.insert(0, str(_HERE))

import threading

from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402

try:  # package import (repo root: `from hub.server import create_app`)
    from . import animations as _animations
    from . import body as _body
    from . import brain as _brain
    from . import clients as _clients
    from . import emotion as _emotion
    from . import intents as _intents
    from . import memory as _memory
    from . import speech as _speech
    from . import touch as _touch
    from . import voice as _voice
    from .clients import ClientHub
    from .events import EventBus
    from .mcpmanager import (MCPManager, public_servers as public_mcp_servers,
                             sdk_available as mcp_available,
                             validate_servers as validate_mcp_servers)
    from .toolloop import run_turn
    from . import spotify as _spotify
    from .tools import weather as _weather
    from .tools.memory_tools import build_memory_specs
    from .tools.registry import build_hub_specs, hub_tool_info, permission_for, tools_prompt
    from .tools.spotify_tools import build_spotify_specs
    from .tools import timers as _timers
    from .tools import animation_tools as _anim_tools
    from .scheduler import TimerScheduler, TimerPermanent
    from .store import Store, default_voice, now_iso
except ImportError:  # standalone: modules sit next to this file (Docker: /app)
    import animations as _animations  # type: ignore[no-redef]
    import body as _body  # type: ignore[no-redef]
    import brain as _brain  # type: ignore[no-redef]
    import clients as _clients  # type: ignore[no-redef]
    import emotion as _emotion  # type: ignore[no-redef]
    import intents as _intents  # type: ignore[no-redef]
    import memory as _memory  # type: ignore[no-redef]
    import speech as _speech  # type: ignore[no-redef]
    import touch as _touch  # type: ignore[no-redef]
    import voice as _voice  # type: ignore[no-redef]
    from clients import ClientHub  # type: ignore[no-redef]
    from events import EventBus  # type: ignore[no-redef]
    from mcpmanager import (MCPManager,  # type: ignore[no-redef]
                            public_servers as public_mcp_servers,
                            sdk_available as mcp_available,
                            validate_servers as validate_mcp_servers)
    from toolloop import run_turn  # type: ignore[no-redef]
    import spotify as _spotify  # type: ignore[no-redef]
    from tools import weather as _weather  # type: ignore[no-redef]
    from tools.memory_tools import build_memory_specs  # type: ignore[no-redef]
    from tools.registry import (build_hub_specs, hub_tool_info,  # type: ignore[no-redef]
                                permission_for, tools_prompt)
    from tools.spotify_tools import build_spotify_specs  # type: ignore[no-redef]
    from tools import timers as _timers  # type: ignore[no-redef]
    from tools import animation_tools as _anim_tools  # type: ignore[no-redef]
    from scheduler import TimerScheduler, TimerPermanent  # type: ignore[no-redef]
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
MAX_TTS_TEXT = 5000                             # POST /api/tts raw-text cap
# (what actually gets spoken is capped by speech.MAX_SPOKEN_CHARS)
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
    # Weather unit preset (docs/API.md "Weather"): auto (follows the saved
    # location's country) | metric | uk | us; legacy imperial -> us. The
    # per-quantity overrides (blank = from the preset): units_temperature
    # celsius|fahrenheit, units_wind kmh|mph|ms|kn, units_precipitation mm|inch.
    "units": "auto",
    "units_temperature": "", "units_wind": "", "units_precipitation": "",
    "permissions": {},        # tool name -> always | ask | never
}

# Location settings (weather round, docs/API.md "Weather"). The saved object
# holds the resolved place {name, latitude, longitude, timezone, country,
# admin1}; blank/reset = the RIKO_LOCATION env default (a plain place name,
# geocoded lazily at first use and cached) or no location at all.
LOCATION_PLACE_KEYS = ("name", "latitude", "longitude", "timezone",
                       "country", "country_code", "admin1")

# MCP settings (docs/ROUNDS.md R2-M): no servers out of the box; the only
# env default is the built-in Home Assistant server composed in main() from
# RIKO_HA_URL + RIKO_HA_TOKEN (see create_app's mcp_defaults).

# Long-term memory settings (docs/WALK_MEMORY.md M). No env defaults: the
# block is exactly the built-in defaults plus what the owner saved.
MEMORY_DEFAULTS = {
    "enabled": True,             # off = no prompt block, no memory tools
    "prompt_budget_tokens": 600,  # cap for the "What you remember" block
    "auto_remember": True,       # false = only remember when explicitly asked
}

# Timer settings (docs/TIMERS.md "Reviewer additions"). Blank = default:
# timer-FIRED turns do not get the `timer` tool by default, so a fired
# reminder cannot schedule another one forever (chain guard).
TIMER_SETTINGS_DEFAULTS = {
    "allow_chaining": False,
}

# Touch settings (docs/TOUCH.md): the global reaction policy for
# POST /api/touch — mode off|quiet|chatty, seconds between spoken reactions,
# spoken reactions per rolling 10 minutes (counted per persona).
TOUCH_SETTINGS_DEFAULTS = dict(_touch.SETTINGS_DEFAULTS)

# Custom animation settings (docs/SELF_ANIMATION.md): auto_keep saves a
# create_animation result directly as kept, skipping the ask-the-owner-
# whether-to-keep step.
ANIMATION_SETTINGS_DEFAULTS = {"auto_keep": False}

# Music phase 1 settings (docs/SPOTIFY_NOTES.md): ONE Spotify connection
# under the owner's Developer Dashboard app (client id only, PKCE — no
# secret). The tokens themselves live in their own store key
# (`spotify_tokens`, written by hub/spotify.py) and NEVER appear in
# GET /api/settings — only these schema keys are exposed. Blank
# accounts_url/api_url = the built-in Spotify hosts; the RIKO_SPOTIFY_*
# env defaults (main()) can point them at the in-repo fake server in tests.
SPOTIFY_DEFAULTS = {
    "enabled": False,
    "client_id": "",            # write-only-ish: a blank edit never wipes it
    "device_name": "Riko",      # the Spotify Connect device = desktop player
    "accounts_url": "",         # blank = https://accounts.spotify.com
    "api_url": "",              # blank = https://api.spotify.com/v1
    # slow now_playing watch while NOT playing (external play detection,
    # docs/SPOTIFY_NOTES.md): seconds; 0 disables, floor 15 s
    "idle_poll_s": _spotify.IDLE_POLL_S,
}

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


def voice_prompt(known_names: list) -> str:
    """System-prompt line for a speech-recognition turn (`voice: true` in
    POST /api/chat, docs/API.md): names get misheard, and she must roll
    with it instead of commenting. The known names ride along so the
    closest match is the name from the hub, not some other spelling."""
    return ("The user is speaking; their words come from speech recognition "
            "and names may be misheard (e.g. Farina for Furina). "
            f"Names you know: {', '.join(known_names) if known_names else 'none listed'}. "
            "Silently assume the closest name you know; never point out or "
            "comment on the misspelling.")


# Refusal guard (docs/API.md "Claimed-action guard" -> refusal twin): the
# intent kind -> names of tools that can actually perform it. Hub tools
# match exactly; client/MCP tools by `__`-suffix (desktop__switch_workspace,
# mcp_homeassistant__turn_on, …). The hub re-nudges a refusal ONLY when the
# matching tool was offered this turn and not called.
_REFUSAL_TOOL_HINTS = {
    "workspace": ("switch_workspace",),
    "open": ("open_app",),
    "focus": ("focus_window",),
    "window": ("focus_window", "open_app"),
    "timer": ("timer",),
}


def refusal_tool_offered(kind: str | None, offered_names: list) -> bool:
    """True when the turn offered a tool able to perform the requested
    action kind (see _REFUSAL_TOOL_HINTS; device kinds ride on the Home
    Assistant / smart-home tools)."""
    if not kind:
        return False
    names = [str(n).lower() for n in offered_names]
    if kind == "device":
        return any("homeassistant" in n or "home_assistant" in n
                   or n.endswith(("__turn_on", "__turn_off", "__toggle",
                                  "__call_service", "__set_state",
                                  "__light", "__scene"))
                   for n in names)
    hints = _REFUSAL_TOOL_HINTS.get(kind)
    if not hints:
        return False
    return any(n in hints or n.endswith("__" + h) for n in names for h in hints)


def create_app(data_dir, api_key: str | None = None,
               llm_defaults: dict | None = None,
               tts_defaults: dict | None = None,
               tools_defaults: dict | None = None,
               memory_defaults: dict | None = None,
               mcp_defaults: dict | None = None,
               timers_defaults: dict | None = None,
               location_defaults: dict | None = None,
               spotify_defaults: dict | None = None) -> FastAPI:
    """Build the hub app over `data_dir` (SQLite + avatars live there).

    api_key None/empty disables auth. llm_defaults fills any LLM settings
    field the user has not saved via PUT /api/settings (api_key included);
    saved values always win. tts_defaults / tools_defaults do the same for
    the `tts` / `tools` settings objects (RIKO_TTS_* / RIKO_SEARXNG_URL).
    mcp_defaults {"ha_url","ha_token"} (RIKO_HA_URL / RIKO_HA_TOKEN) offers
    the built-in Home Assistant MCP server while no server named
    'homeassistant' is saved (see mcp_servers)."""
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
    memory_defaults = dict(memory_defaults or {})
    mcp_defaults = dict(mcp_defaults or {})
    timers_defaults = dict(timers_defaults or {})
    location_defaults = dict(location_defaults or {})
    spotify_defaults = dict(spotify_defaults or {})
    app = FastAPI(title="riko-hub", version=HUB_VERSION, docs_url=None, redoc_url=None)
    # one chat turn at a time PER PERSONA (docs/TIMERS.md H): a fired timer
    # waits for its own persona's chat to free without blocking the others
    chat_locks: dict[str, threading.Lock] = {}
    chat_locks_guard = threading.Lock()
    events = EventBus()           # GET /api events ring buffer (R1-B)
    clients_hub = ClientHub(events)  # client-tool channel server side (R1-B)

    def persona_lock_for(pid: str) -> threading.Lock:
        with chat_locks_guard:
            lock = chat_locks.get(pid)
            if lock is None:
                lock = chat_locks[pid] = threading.Lock()
            return lock

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
    _TOOLS_BLANK_MEANS_UNSET = ("searxng_url", "units", "units_temperature",
                                "units_wind", "units_precipitation")

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
        # normalize on read: a pre-preset "imperial" still on disk (or in an
        # env default) answers as us; junk answers as the auto default
        merged["units"] = _weather.normalize_preset(merged.get("units"))
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

    # ── memory settings (docs/WALK_MEMORY.md M) ─────────────────────────
    # No blank-means-unset keys: every memory key is a bool/int, and a blank
    # in the page just means "don't change it" (the page omits the key).
    def stored_memory() -> dict:
        saved = store.get_json("memory", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def default_memory() -> dict:
        merged = dict(MEMORY_DEFAULTS)
        merged.update(memory_defaults)
        merged["enabled"] = bool(merged.get("enabled"))
        merged["auto_remember"] = bool(merged.get("auto_remember", True))
        return merged

    def memory_settings() -> dict:
        merged = default_memory()
        merged.update(stored_memory())
        return merged

    # ── timer settings (docs/TIMERS.md "Reviewer additions") ────────────
    # blank = default semantics: allow_chaining defaults False, so fired
    # turns never get the `timer` tool unless the owner turns it on.
    def stored_timers() -> dict:
        saved = store.get_json("timers_settings", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def default_timers() -> dict:
        merged = dict(TIMER_SETTINGS_DEFAULTS)
        for key in TIMER_SETTINGS_DEFAULTS:
            if key in timers_defaults:
                merged[key] = timers_defaults[key]
        merged["allow_chaining"] = bool(merged.get("allow_chaining"))
        return merged

    def timers_settings() -> dict:
        merged = default_timers()
        saved = stored_timers()
        for key in TIMER_SETTINGS_DEFAULTS:
            if key in saved:
                merged[key] = bool(saved[key])
        return merged

    # ── touch settings (docs/TOUCH.md) ─────────────────────────────────
    # One global policy (the web Settings card + the desktop tray write it);
    # the 10-minute rate window is COUNTED per persona inside the route.
    def stored_touch() -> dict:
        saved = store.get_json("touch", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def default_touch(mode: str = "quiet") -> dict:
        return _touch.defaults_for(mode)

    def touch_settings() -> dict:
        saved = stored_touch()
        mode = saved.get("mode") if saved.get("mode") in _touch.MODES else "quiet"
        merged = default_touch(mode)          # per-mode defaults (docs/TOUCH.md)
        for key in ("cooldown_s", "max_per_10min"):
            raw = saved.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
                merged[key] = raw
        return merged

    # ── custom animation settings (docs/SELF_ANIMATION.md) ──────────────
    # auto_keep False (default): create_animation saves a draft and she asks
    # the owner whether to keep it; True: it goes straight to kept.
    def stored_animations() -> dict:
        saved = store.get_json("animations_settings", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def default_animations() -> dict:
        return dict(ANIMATION_SETTINGS_DEFAULTS)

    def animations_settings() -> dict:
        merged = default_animations()
        saved = stored_animations()
        if isinstance(saved.get("auto_keep"), bool):
            merged["auto_keep"] = saved["auto_keep"]
        return merged

    # model identity for animations: sha256 of the persona's stored VRM bytes
    # (the exact file GET /api/personas/{id}/avatar serves), 'default' without
    # an uploaded avatar. Lazily hashed, cached by (mtime, size).
    anim_sha = _animations.ModelShaResolver(avatars_dir)

    # hold-semantics migration (docs/SELF_ANIMATION.md): stored animations
    # predating the 2026-11 revision leave bones/expression/hips_y out of
    # keyframes they hold through. Expand every stored row to explicit
    # per-key values ONCE at start so the page player never needs a special
    # case and every draft on the live hub (e.g. spotlight_twirl) becomes
    # unambiguous without a manual step. Idempotent — re-running changes 0.
    try:
        _kf_fixed = _animations.migrate_hold_semantics(store)
        if _kf_fixed:
            print(f"[hub] hold-semantics keyframe migration rewrote "
                  f"{_kf_fixed} animation row(s)", flush=True)
    except Exception as e:  # never block hub start on the migration
        print(f"[hub] hold-semantics keyframe migration skipped: "
              f"{type(e).__name__}: {e}", flush=True)

    # ── location settings (weather round, docs/API.md "Weather") ────────
    # Saved place wins; blank/reset falls back to the RIKO_LOCATION env
    # default (a plain place name geocoded lazily at first use, cached in
    # hub.tools.weather) or, with no env default, no location at all.
    def stored_location() -> dict:
        saved = store.get_json("location", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def default_location() -> dict:
        # `name` is the UNRESOLVED env default (shown on the page; resolved
        # lazily by resolve_location, never by GET /api/settings). Weather
        # units are the separate `tools.units` setting.
        return {"name": str(location_defaults.get("place") or "").strip()}

    def location_settings() -> dict:
        merged = default_location()
        merged.update(stored_location())
        return merged

    def resolve_location() -> dict | None:
        """The place weather/prompt questions resolve to: saved fields, else
        the env default geocoded on first use (10-min cache). None = none.
        Geocoding failure returns None, never raises (GET /api/state and the
        chat prompt both call this on hot paths)."""
        saved = stored_location()
        if saved.get("latitude") is not None and saved.get("longitude") is not None:
            return saved
        env = str(location_defaults.get("place") or "").strip()
        if not env:
            return None
        try:
            return _weather.geocode_first(env)
        except Exception:
            return None

    def location_label_name() -> str:
        """Name for tool progress labels — never touches the network."""
        return (str(stored_location().get("name") or "").strip()
                or str(location_defaults.get("place") or "").strip())

    def units_effective() -> dict:
        """The units the Weather tool answers in right now (docs/API.md
        "Weather"), for the Settings page line "currently: °C · mph · mm".
        `auto` follows the SAVED place's country; a lazy RIKO_LOCATION env
        default is NOT geocoded here (GET /api/settings never touches the
        network) and reads as metric."""
        cfg = tools_settings()
        saved = stored_location()
        loc = saved if saved.get("latitude") is not None else None
        return _weather.unit_symbols(_weather.resolve_units(
            cfg.get("units"),
            {k: cfg.get("units_" + k) for k in
             ("temperature", "wind", "precipitation")}, loc))

    # ── MCP settings + manager (docs/ROUNDS.md R2-M) ───────────────────
    # settings `mcp.servers` env default: the built-in Home Assistant server
    # when RIKO_HA_URL + RIKO_HA_TOKEN are set (mcp_defaults). The manager
    # owns one asyncio loop thread for all sessions; the hub boots fine
    # without the mcp package (servers just report state "unavailable").
    mcp_manager = MCPManager(events)

    def stored_mcp() -> dict:
        saved = store.get_json("mcp", {})
        return dict(saved) if isinstance(saved, dict) else {}

    def ha_preset() -> dict | None:
        """The built-in 'homeassistant' MCP server (HA's official Model
        Context Protocol Server integration, /api/mcp/assist = the Assist
        device-control toolset). Env-default semantics like the other
        setting blocks: shown while nothing named 'homeassistant' is saved,
        editable/disableable on the page, and reset returns to it. The
        Authorization header keeps the ${RIKO_HA_TOKEN} placeholder — the
        token itself stays in the hub environment, never in the database."""
        url = str(mcp_defaults.get("ha_url") or "").strip().rstrip("/")
        token = str(mcp_defaults.get("ha_token") or "").strip()
        if not url or not token:
            return None
        return {"name": "homeassistant", "transport": "http", "enabled": True,
                "url": url + "/api/mcp/assist",
                "headers": {"Authorization": "Bearer ${RIKO_HA_TOKEN}"},
                "default_permission": "always"}

    def mcp_servers() -> list[dict]:
        servers = stored_mcp().get("servers")
        out = [s for s in servers if isinstance(s, dict)] \
            if isinstance(servers, list) else []
        preset = ha_preset()
        if preset and not any(s.get("name") == "homeassistant" for s in out):
            out = out + [preset]
        return out

    # ── spotify settings + hub (music phase 1, docs/SPOTIFY_NOTES.md) ──
    # One Spotify connection, tokens in the store key `spotify_tokens`
    # (write-only: GET /api/settings never sees them, /api/spotify/status
    # reports booleans/expiry only). SpotifyHub owns the quota back-off,
    # device cache and the now_playing poller.
    def stored_spotify() -> dict:
        saved = dict(store.get_json("spotify", {}))
        for key in ("accounts_url", "api_url"):
            if key in saved and not str(saved[key] or "").strip():
                del saved[key]
        if "client_id" in saved and not str(saved["client_id"] or "").strip():
            del saved["client_id"]      # legacy blank = unset, like api_key
        return saved

    def default_spotify() -> dict:
        merged = dict(SPOTIFY_DEFAULTS)
        merged.update(spotify_defaults)
        merged["enabled"] = bool(merged.get("enabled"))
        merged["device_name"] = (str(merged.get("device_name") or "").strip()
                                 or "Riko")
        return merged

    def spotify_settings() -> dict:
        # projected onto the schema keys: junk (or a stray token) saved into
        # the block by an older/newer client never rides along on reads
        merged = default_spotify()
        saved = stored_spotify()
        for key in SPOTIFY_DEFAULTS:
            if key in saved:
                merged[key] = saved[key]
        return merged

    def _spotify_desktop_online() -> bool:
        """Idle-watch interest (docs/SPOTIFY_NOTES.md): a registered desktop
        client heartbeated within the offline window — the ticker only
        matters to someone when the avatar's machine is on the hub."""
        return any(c.get("kind") == "desktop"
                   for c in clients_hub.online_clients())

    spotify_hub = _spotify.SpotifyHub(store, events, spotify_settings,
                                      defaults=spotify_defaults,
                                      on_change=bump_state,
                                      interest_hook=_spotify_desktop_online)

    def spotify_error_response(e: Exception) -> JSONResponse:
        """Route-side mapping of hub/spotify.py errors (docs/API.md)."""
        if isinstance(e, _spotify.SpotifyQuota):
            return err(429, str(e))
        if isinstance(e, _spotify.SpotifyPremium):
            return err(403, str(e))
        if isinstance(e, (_spotify.SpotifyNotConfigured,
                          _spotify.SpotifyNotConnected)):
            return err(409, str(e))
        return err(400, str(e))

    # ── timezone setting (docs/TIMERS.md H) — blank = server TZ ─────────
    def stored_timezone() -> str:
        """Saved IANA zone name, '' when unset (unset = server default)."""
        return str(store.get_json("timezone", "") or "").strip()

    def default_timezone() -> str:
        return _timers.default_timezone_name()

    def location_timezone() -> str:
        """IANA zone of the resolved location ('' when none)."""
        loc = resolve_location() or {}
        return str(loc.get("timezone") or "").strip()

    def effective_timezone():
        """ZoneInfo the hub speaks times in: saved → the location's zone
        (weather round: an owner who picked a place wants local times for
        it) → TZ env → UTC."""
        saved = stored_timezone()
        if saved:
            tz = _timers.resolve_timezone(saved)
            if tz is not None:
                return tz
        else:
            loc_tz = location_timezone()
            if loc_tz:
                tz = _timers.resolve_timezone(loc_tz)
                if tz is not None:
                    return tz
        return _timers.resolve_timezone(default_timezone())

    def effective_timezone_name() -> str:
        tz = effective_timezone()
        if tz is not None:
            return getattr(tz, "key", "") or str(tz)
        return "UTC"

    def due_at_local_from_utc_str(due_str: str) -> str:
        """Render a stored due_at (UTC ISO) in the hub's configured zone."""
        due = None
        try:
            due = datetime.strptime(str(due_str), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            try:
                due = datetime.fromisoformat(str(due_str).replace("Z", "+00:00"))
            except ValueError:
                return str(due_str)
        return _timers.to_local(due, effective_timezone())

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
        preset = ha_preset()
        saved_memory = stored_memory()
        memory_source = {k: ("saved" if k in saved_memory else "default")
                         for k in MEMORY_DEFAULTS}
        saved_timers = stored_timers()
        timers_source = {k: ("saved" if k in saved_timers else "default")
                         for k in TIMER_SETTINGS_DEFAULTS}
        saved_loc = stored_location()
        env_place = str(location_defaults.get("place") or "").strip()
        location_source = {}
        for k in LOCATION_PLACE_KEYS:
            if k in saved_loc:
                location_source[k] = "saved"
            elif k == "name" and env_place:
                location_source[k] = "env"      # the RIKO_LOCATION default
            else:
                location_source[k] = "default"  # default = no location
        saved_spotify = stored_spotify()
        spotify_source = {k: ("saved" if k in saved_spotify else "default")
                          for k in SPOTIFY_DEFAULTS}
        saved_touch = stored_touch()
        touch_source = {k: ("saved" if k in saved_touch else "default")
                        for k in TOUCH_SETTINGS_DEFAULTS}
        saved_anim = stored_animations()
        animations_source = {k: ("saved" if k in saved_anim else "default")
                             for k in ANIMATION_SETTINGS_DEFAULTS}
        return {
            "llm": public_llm(llm_settings()),
            "tts": tts_settings(),
            "tools": tools_settings(),
            "memory": memory_settings(),
            "timers": timers_settings(),
            "touch": touch_settings(),
            "animations": animations_settings(),
            "mcp": {"servers": public_mcp_servers(mcp_servers())},
            "switch_phrases": switch_phrases(),
            "timezone": stored_timezone(),
            "timezone_source": "saved" if stored_timezone() else "default",
            "location": location_settings(),
            "location_source": location_source,
            "units_effective": units_effective(),
            # music phase 1: schema keys only — the tokens live in another
            # store key and can NEVER ride along here
            "spotify": spotify_settings(),
            "spotify_source": spotify_source,
            "spotify_connected": spotify_hub.connected(),
            "defaults": {"llm": defaults_public,
                         "tts": default_tts(),
                         "tools": default_tools(),
                         "memory": default_memory(),
                         "timers": default_timers(),
                         "touch": default_touch(str(
                             (store.get_json("touch", {}) or {}).get("mode")
                             or "quiet")),
                         "animations": default_animations(),
                         "mcp": {"servers": public_mcp_servers([preset])
                                 if preset else []},
                         "switch_phrases": list(_brain.DEFAULT_SWITCH_PHRASES),
                         "timezone": default_timezone(),
                         "location": default_location(),
                         "spotify": default_spotify()},
            "llm_source": source,
            "tts_source": tts_source,
            "tools_source": tools_source,
            "memory_source": memory_source,
            "timers_source": timers_source,
            "touch_source": touch_source,
            "animations_source": animations_source,
            "mcp_source": {"servers": "saved" if has_mcp
                           else ("env" if preset else "default")},
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
        # timers round: open timers die with the persona (docs/TIMERS.md)
        doomed_timers = store.find_open_timers(persona_id)
        fact_ids = store.delete_persona(persona_id)
        (avatars_dir / f"{persona_id}.vrm").unlink(missing_ok=True)
        reference_path(persona_id).unlink(missing_ok=True)
        delete_message_audio(message_ids)
        delete_persona_audio(persona_id)
        # memory round: the persona's private notes died with it
        for fid in fact_ids:
            events.publish("memory_changed", id=fid, op="deleted")
        # timers round: their open timers died with it too
        for t in doomed_timers:
            _publish_cancelled(t, "persona_deleted")
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
            before = int(request.query_params.get("before") or 0)
            limit = min(500, max(1, int(request.query_params.get("limit") or 50)))
        except ValueError:
            return err(400, "after, before and limit must be integers")
        if before > 0:
            messages = store.messages_before(pid, before, limit)
        else:
            messages = store.messages_after(pid, after, limit)
        # rows stored before the spoken-text round have speech ''; fill it so
        # clients doing their own TTS always get clean words, never a URL.
        for m in messages:
            if m["role"] == "assistant" and not m["speech"]:
                m["speech"] = _speech.speech_text(m["content"])
        return {"persona": pid, "messages": messages}

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

    # ── per-message history edits (select + delete / save to memory) ───
    def publish_history_deleted(pid: str, ids: list[int]) -> None:
        events.publish("history_changed", persona=pid, ids=ids, op="deleted")
        bump_state()

    @app.delete("/api/history/{message_id}", status_code=204)
    def delete_history_message(message_id: int, request: Request):
        """Delete one stored message. ?persona= (default: the message's own)
        guards against removing another persona's row: mismatch → 404."""
        msg = store.get_message(message_id)
        if not msg:
            return err(404, f"no message {message_id}")
        wanted = request.query_params.get("persona")
        if wanted and wanted != msg["persona"]:
            return err(404, f"message {message_id} belongs to '{msg['persona']}'")
        delete_message_audio([message_id])  # cached audio goes with the row
        store.delete_messages([message_id])
        publish_history_deleted(msg["persona"], [message_id])
        return Response(status_code=204)

    @app.post("/api/history/delete")
    async def post_history_delete(request: Request):
        """Bulk delete: {persona?, ids: [...]}. persona defaults to the active
        one; ids of other personas (or already gone) are skipped and reported:
        {deleted: n, skipped: [ids]}. Takes the messages' cached audio too."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        pid = str(body.get("persona") or "") or store.active()
        if not store.persona_exists(pid):
            return err(404, f"no persona '{pid}'")
        raw_ids = body.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return err(422, "ids must be a non-empty list of message ids")
        wanted: list[int] = []
        for raw in raw_ids[:5000]:
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                return err(422, "ids must be integers")
            if mid not in wanted:
                wanted.append(mid)
        deletable, skipped = [], []
        for mid in wanted:
            msg = store.get_message(mid)
            if not msg or msg["persona"] != pid:
                skipped.append(mid)  # missing, or another persona's row
            else:
                deletable.append(mid)
        if deletable:
            delete_message_audio(deletable)
            store.delete_messages(deletable)
            publish_history_deleted(pid, deletable)
        return {"deleted": len(deletable), "skipped": skipped}

    @app.post("/api/history/{message_id}/remember", status_code=201)
    async def post_history_remember(message_id: int, request: Request):
        """Save a message to long-term memory (the page's 'Add to memory'):
        {text?, scope?: 'persona'|'global'}. Default text = the message's own
        content clamped like any fact; default scope = persona (a private
        note about who was talking). Same dedup/budget path as POST
        /api/memory; the fact records source_message_id for provenance."""
        msg = store.get_message(message_id)
        if not msg:
            return err(404, f"no message {message_id}")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        scope_kind = str(body.get("scope") or "persona")
        if scope_kind == "global":
            scope = "user"
        elif scope_kind == "persona":
            scope = f"persona:{msg['persona']}"
        else:
            return err(422, "scope must be 'persona' or 'global'")
        if not valid_fact_scope(scope):
            return err(422, f"no persona for scope '{scope}'")
        text = body.get("text")
        text = _memory.clamp_fact_text(
            msg["content"] if text is None or not str(text).strip() else text)
        if not text:
            return err(422, "message has no text to remember")
        fact, updated = store.upsert_fact(text, scope=scope,
                                          source_message_id=message_id)
        events.publish("memory_changed", id=fact["id"],
                       op="updated" if updated else "added")
        bump_state()
        return {**fact, "updated": updated}

    # ── timers (docs/TIMERS.md H) ───────────────────────────────────────
    # The set-turn captures what the fire-turn must run with: merged LLM
    # settings, memory settings, and the ids of the tools this turn offered.
    # Timers speak the hub's configured timezone (saved `timezone` setting →
    # TZ env → UTC).
    def create_timer(persona_id: str, prompt: str, time_raw, label: str,
                     cancel_on: list, snapshot: dict, asked: bool = False,
                     chain_depth: int = 0) -> dict:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("timer_error: 'prompt' is required")
        if len(prompt) > 4000:
            prompt = prompt[:4000]
        # chain guard (docs/TIMERS.md "Reviewer additions"): a timer created
        # during a fired timer's turn inherits the parent depth + 1 and is
        # refused past MAX_CHAIN_DEPTH, so chained reminders cannot run away.
        if chain_depth > _timers.MAX_CHAIN_DEPTH:
            raise ValueError(
                "timer_error: chain limit — this reminder has already "
                f"chained {_timers.MAX_CHAIN_DEPTH} timers; finish the chain "
                "instead of scheduling another")
        if store.open_timer_count() >= _timers.MAX_PENDING_TIMERS:
            raise ValueError(
                f"timer_error: at most {_timers.MAX_PENDING_TIMERS} pending "
                "timers — cancel one first")
        tz = effective_timezone()
        now_utc = datetime.now(timezone.utc)
        due = _timers.parse_timer_time(time_raw, now_utc)
        captured = dict(snapshot or {})
        if asked:
            captured["asked"] = True
        if chain_depth:
            captured["chain_depth"] = int(chain_depth)
        row = store.add_timer(persona_id=persona_id,
                              label=(label or _timers.label_from_prompt(prompt))[:80],
                              prompt=prompt,
                              due_at=due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              cancel_on=cancel_on, captured=captured)
        if row is None:
            raise RuntimeError("timer_error: could not store the timer")
        local = due if tz is None else due.astimezone(tz)
        row["due_at_local"] = _timers.to_local(due, tz)
        row["timezone"] = effective_timezone_name() if tz is not None else "UTC"
        row["local_time"] = local.strftime("%Y-%m-%d %H:%M")
        events.publish("timer_set", id=row["id"], label=row["label"],
                       persona_id=row["persona_id"], due_at=row["due_at"],
                       due_at_local=row["due_at_local"], cancel_on=row["cancel_on"],
                       asked=bool(asked), chain_depth=int(chain_depth))
        bump_state()
        return row

    def _publish_cancelled(row: dict, reason: str) -> None:
        events.publish("timer_cancelled", id=row["id"], label=row["label"],
                       persona_id=row["persona_id"], reason=reason)

    def cancel_timers_on_event(persona_id: str, event: str, reason: str) -> list:
        """cancel_on hook: cancel this persona's open timers listening for
        `event` (claimed-but-unfired rows count — the scheduler skips them
        after it wins the persona lock)."""
        rows = store.cancel_timer_rows(
            [t["id"] for t in store.find_open_timers(persona_id, event)])
        for row in rows:
            _publish_cancelled(row, reason)
        if rows:
            bump_state()
        return rows

    def cancel_timer_for_persona(persona_id: str, tid, label: str) -> dict:
        """cancel_timer tool + DELETE route core: id wins over label; label
        matches exact (case-insensitive) first, then substring."""
        rows = store.find_open_timers(persona_id)
        if tid not in (None, ""):
            try:
                tid_i = int(tid)
            except (TypeError, ValueError):
                return {"error": "timer_error: id must be a number"}
            row = next((r for r in rows if r["id"] == tid_i), None)
            if row is None:
                return {"error": f"timer_error: no open timer with id {tid_i}"}
        else:
            lab = (label or "").strip().lower()
            matches = [r for r in rows if r["label"].lower() == lab] \
                or [r for r in rows if lab and lab in r["label"].lower()]
            if not matches:
                return {"error": f"timer_error: no open timer labelled {label!r}"}
            if len(matches) > 1:
                return {"error": "timer_error: several open timers match that "
                                 "label — cancel by id",
                        "candidates": [{"id": m["id"], "label": m["label"]}
                                       for m in matches]}
            row = matches[0]
        done = store.cancel_timer_rows([row["id"]])
        for r in done:
            _publish_cancelled(r, "tool")
        if done:
            bump_state()
            return {"cancelled": row["id"], "label": row["label"]}
        return {"error": f"timer_error: timer {row['id']} is no longer open"}

    timers_api = {"create": create_timer, "cancel": cancel_timer_for_persona,
                  "timezone": effective_timezone_name,
                  "local": due_at_local_from_utc_str}

    def fire_timer_turn(timer: dict) -> dict:
        """Scheduler callback: run the timer prompt as a normal chat turn
        (emotion, actions, speech, audio, memory and tools all apply), under
        the persona lock the scheduler already holds."""
        kind, *rest = run_chat_turn(timer["persona_id"], timer["prompt"],
                                    "timer", True, False, timer=timer)
        if kind == "err":
            code, detail = rest
            if code == 404:  # persona vanished — retrying cannot help
                raise TimerPermanent(str(detail))
            raise RuntimeError(f"HTTP {code}: {detail}")
        data = rest[0]
        return {"message_id": data["message_ids"][-1], "reply": data["reply"],
                "speech": data["speech"], "emotion": data["emotion"],
                "actions": data["actions"], "audio_url": data["audio_url"]}

    def run_chat_turn(pid: str, text: str, source: str, speak: bool,
                      voice_flag: bool, timer: dict | None = None):
        """One normal chat turn for persona `pid` — the shared body of
        POST /api/chat and of fired timers (docs/TIMERS.md: same path, so a
        timer turn gets emotion, actions, speech, audio_url, memory and
        tools). A timer turn runs with the SET-turn's captured settings and
        tool ids and must not be cancelled by its own user message (the
        cancel hook lives in post_chat, not here). Caller holds
        persona_lock_for(pid). Returns ("ok", data) | ("err", code, detail).
        """
        persona = store.get_persona(pid)
        if not persona:
            return ("err", 404, f"no persona '{pid}'")
        captured = (timer or {}).get("captured") or {}
        settings = captured.get("settings") or llm_settings()
        if not settings.get("base_url"):
            return ("err", 503, "LLM not configured")

        # docs/TIMERS.md "Reviewer additions": per-turn creation trace (the
        # asked-for fallback must tell "the model set one" from "she only
        # claimed one", and the timers_set result field lists both).
        turn_created: list = []
        # chain guard: a timer created during a fired timer's turn inherits
        # the parent's chain_depth + 1 (create_timer refuses past the cap)
        parent_depth = int(captured.get("chain_depth") or 0) if timer else 0

        def create_for_turn(**kw):
            row = create_timer(**kw,
                               chain_depth=(parent_depth + 1 if timer else 0))
            turn_created.append(row)
            return row

        turn_timers_api = dict(timers_api, create=create_for_turn)
        # the asked-for matcher never runs on a timer-fired turn: its "user"
        # message is the timer's own prompt, not a fresh ask
        asked_timer = None
        if timer is None:
            asked_timer = _timers.detect_timer_request(
                text, now=datetime.now(timezone.utc),
                tz_name=effective_timezone_name())

        # offered tools: hub built-ins + online client tools, minus anything
        # the permission table says `never` (docs/ROUNDS.md)
        tools_cfg = tools_settings()
        mem_cfg = captured.get("memory") or memory_settings()
        memory_writes: list = []
        specs: list = []
        turn_tool_ids: list = []
        timer_capture: dict = {"settings": settings, "memory": mem_cfg,
                               "tool_ids": []}
        # create_animation drops its preview play action here (server adds
        # it to the reply's actions — docs/SELF_ANIMATION.md)
        anim_capture: dict = {}
        if tools_cfg.get("enabled"):
            permissions = tools_cfg.get("permissions") or {}
            hub_specs = build_hub_specs(
                tools_cfg,
                location_ctx={"resolve": resolve_location,
                              "label_name": location_label_name})
            # memory round (docs/WALK_MEMORY.md M): remember/recall/
            # forget_fact ride the tool loop, default permission always,
            # and vanish when memory or tools are off
            if mem_cfg.get("enabled"):
                hub_specs += build_memory_specs(
                    store, events, pid, memory_writes,
                    auto_remember=bool(mem_cfg.get("auto_remember", True)))
            # timers round (docs/TIMERS.md H): timer / list_timers /
            # cancel_timer, same per-turn build as the memory tools.
            # timer_capture is filled in once the turn's offered tool ids
            # are known; a timer set during this turn stores it as `captured`.
            # turn_timers_api also records creations for the fallback.
            hub_specs += _anim_tools.build_animation_specs(
                store, pid,
                {"sha": lambda: anim_sha(pid),
                 "auto_keep": lambda: bool(
                     animations_settings().get("auto_keep"))},
                anim_capture, events=events)
            hub_specs += _timers.build_timer_specs(store, pid, turn_timers_api,
                                                   lambda: timer_capture)
            # music phase 1 (docs/SPOTIFY_NOTES.md): the spotify_* player
            # tools ride the same per-turn build; they are LISTED while the
            # tools are on but only OFFERED once Spotify is configured
            # (ToolSpec.available -> toolloop/tool_prompt filtering).
            hub_specs += build_spotify_specs(spotify_hub, tools_cfg)
            for s in hub_specs:
                if permission_for(s.name, "hub", s.risk, permissions) != "never":
                    specs.append(s)
            for s in clients_hub.online_tool_specs():
                if permission_for(s.name, "client", s.risk, permissions) != "never":
                    specs.append(s)
            # MCP tools (R2-M): mcp_<server>__<tool>, the manager's
            # per-server tool_permissions override beats the map, the
            # per-server default_permission beats the risk fallback
            for s in mcp_manager.tool_specs():
                if permission_for(s.name, "mcp", s.risk, permissions,
                                  s.permission_override,
                                  s.permission_default) != "never":
                    specs.append(s)
            if captured.get("tool_ids") is not None:
                # fired timer: only the tools the set-turn had offered
                allowed = set(captured["tool_ids"])
                specs = [s for s in specs if s.name in allowed]
        if timer is not None and not timers_settings().get("allow_chaining"):
            # chain guard (docs/TIMERS.md "Reviewer additions"): a fired
            # timer's turn answers, it does not schedule — the `timer` tool
            # stays off the offer unless the owner enabled
            # timers.allow_chaining (list_timers/cancel_timer remain)
            specs = [s for s in specs if s.name != "timer"]
        offered_names = [s.name for s in specs if s.available]
        turn_tool_ids.extend(offered_names)
        timer_capture["tool_ids"] = list(offered_names)

        system = persona["system_prompt"]
        if persona.get("emotes"):
            system += "\n\n" + _emotion.PROMPT_RULE
        # docs/BODY.md: the body block only when an embodied client is online
        # AND the persona is embodied; the appearance line always (text-only
        # chats can describe her too).
        body_actions = clients_hub.body_actions()
        # kept custom animations on the model the clients are wearing
        # (docs/SELF_ANIMATION.md): [act:<name>] tags resolve against these
        custom_moves = sorted(r["name"] for r in store.list_animations(
            pid, anim_sha(pid), status="kept"))
        if persona.get("embodied", True) and body_actions:
            system += "\n\n" + _body.body_prompt(body_actions,
                                                  custom_actions=custom_moves)
            # one-line nudge when this very message asks for a gesture:
            # cheap and raises tag compliance
            asked_for = _body.detect_action_request(text, body_actions)
            if asked_for:
                system += "\n\n" + _body.request_hint(asked_for)
        appearance = str(persona.get("appearance") or "").strip()
        if appearance:
            system += "\n\n" + _body.appearance_prompt(appearance)
        # docs/TIMERS.md "Reviewer additions": the user message clearly asks
        # for a timer — nudge her to call the tool, not just talk about it
        # (same trick as the body request hint).
        if asked_timer:
            system += "\n\n" + _timers.request_hint(asked_timer)
        if voice_flag:  # speech-recognised turn: never comment on misheard names
            system += "\n\n" + voice_prompt(
                [p["name"] for p in store.list_personas()])
        # memory round: the "What you remember" block goes in even when the
        # tool loop is off (facts stay useful as notes); the memory tools
        # themselves only exist while tools are on.
        memory_facts: list = []
        if mem_cfg.get("enabled"):
            ordered_facts = _memory.gather_facts(store, text, pid)
            block, memory_facts = _memory.build_memory_block(
                ordered_facts,
                bool(mem_cfg.get("auto_remember", True)),
                any(s.name == "remember" for s in specs),
                int(mem_cfg.get("prompt_budget_tokens") or 600))
            if block:
                system += "\n\n" + block
                store.touch_facts([f["id"] for f in memory_facts])
        if offered_names:
            # local-time line (docs/TIMERS.md): she speaks times in the
            # hub's configured timezone, never in raw UTC
            tz_now = datetime.now(effective_timezone() or timezone.utc)
            system += "\n\n" + tools_prompt(
                offered_names,
                now_local=tz_now.strftime("%Y-%m-%d %H:%M"),
                tz_name=effective_timezone_name(),
                local_note=("" if timer is None else
                            " A timer you set has just fired: its prompt is the "
                            "next user message — act on it now (remind, check, "
                            "answer); do not schedule it again unless asked"))
            # MCP Prompts (docs/API.md): server-provided instructions
            # (e.g. HA Assist's) + the HA speech hint
            mcp_notes = mcp_manager.prompt_notes(offered_names)
            if mcp_notes:
                system += "\n\n" + mcp_notes
        # weather round (docs/API.md "Weather"): one line naming the owner's
        # place, whether or not tools are on — it also grounds time and
        # local questions. The local-time line above already follows the
        # location zone when settings.timezone is blank (effective_timezone).
        turn_loc = resolve_location()
        if turn_loc and str(turn_loc.get("name") or "").strip():
            system += "\n\n" + _weather.location_prompt(turn_loc)

        # weather pre-fetch (docs/API.md "Weather pre-fetch"): the message
        # asks about the weather, so the hub calls the weather tool itself
        # BEFORE the model and injects the result. The answer becomes one
        # LLM round-trip and cannot be invented from memory; the chip and
        # the tool log still show the fetch (labelled, auto). Skipped when
        # the owner set weather to permission `never`. On failure the turn
        # falls back to the normal tool path plus a hint.
        wx_ask = _intents.detect_weather_request(text)
        wx_prefetch: dict | None = None
        wx_used: dict | None = None
        # permission `never` (weather not offered while tools are on) means
        # the owner does not want weather fetched; with tools off entirely
        # the pre-fetch is the hub's own answer grounding, so it runs
        if wx_ask and (not tools_cfg.get("enabled")
                       or "weather" in offered_names):
            place_ask = str(wx_ask.get("place") or "").strip()
            label = (f"checking the weather in "
                     f"{place_ask or location_label_name() or 'their area'}")
            have_place = bool(place_ask
                              or (turn_loc and turn_loc.get("latitude")
                                  is not None))
            wx_args = {"days": wx_ask["days"]}
            if place_ask:
                wx_args["place"] = place_ask
            if wx_ask["hourly"]:
                wx_args["hourly"] = True
            wx_ctx = {"resolve": resolve_location,
                      "units": _weather.normalize_preset(tools_cfg.get("units"))}
            for _qty in ("temperature", "wind", "precipitation"):
                wx_ctx["units_" + _qty] = str(
                    tools_cfg.get("units_" + _qty) or "").strip().lower()
            call_id = f"hub-prefetch-{int(time.time() * 1000) % 1000000}"
            t0 = time.monotonic()
            if have_place:
                events.publish("tool_started", call_id=call_id,
                               tool="weather", label=label)
            try:
                wx_payload = (_weather.weather_tool(wx_args, wx_ctx)
                              if have_place else None)
                if have_place and wx_payload is None:
                    raise _weather.WeatherError("no location")
                wx_prefetch = wx_payload
                wx_error = ""
            except Exception as e:  # WeatherError or a tool bug: fallback
                wx_error = str(e) or type(e).__name__
            ms = int((time.monotonic() - t0) * 1000)
            if wx_prefetch is not None:
                events.publish("tool_finished", call_id=call_id,
                               tool="weather", ok=True, ms=ms)
                store.log_tool(persona=pid, tool="weather", source="hub",
                               arguments=json.dumps(wx_args), ok=True,
                               error="", ms=ms,
                               approval="auto (hub pre-fetch)")
                system += "\n\n" + _intents.weather_data_block(wx_prefetch)
                place_name = str(((wx_prefetch.get("place") or {})
                                  .get("name") or "").strip()
                                 or place_ask or "their area")
                wx_used = {"tool": "weather",
                           "label": f"checking the weather in {place_name}",
                           "source": "hub", "auto": True}
                log.info("[turn] weather pre-fetch persona=%s place=%s "
                         "days=%d ms=%d", pid, place_name, wx_ask["days"], ms)
            else:
                if have_place:
                    store.log_tool(persona=pid, tool="weather", source="hub",
                                   arguments=json.dumps(wx_args), ok=False,
                                   error=wx_error, ms=ms,
                                   approval="auto (hub pre-fetch)")
                system += "\n\n" + _intents.prefetch_hint(
                    "weather" in offered_names)
                log.info("[turn] weather pre-fetch skipped persona=%s: %s",
                         pid, wx_error or "no location")

        max_history = int(settings.get("max_history") or 40)
        history = store.recent_messages(pid, max_history)

        # workspace pre-act (docs/API.md "Workspace pre-act"): the message
        # imperatively asks to switch workspace/desktop N and an online
        # client offers switch_workspace (permission != never), so the hub
        # performs the switch ITSELF through the client-tool channel BEFORE
        # the model — permission is always for it — and injects the result.
        # She then confirms instead of arguing about keys; a failed switch
        # injects the real error. Number words, ordinals and Whisper
        # homophones are normalised ("And switch to workspace one." ->
        # workspace 1). The bare "switch to N" and a bare repeat ask ("Try
        # that again.") count only when the last assistant turn in this
        # persona chat called switch_workspace; the repeat replays the
        # args stored in that turn's tools_used entry. Skipped on
        # timer-fired turns (their "user" message is the timer prompt,
        # never an owner imperative).
        last_assistant = next((m for m in reversed(history)
                               if m.get("role") == "assistant"), None)
        switch_entries = [
            t for t in ((last_assistant or {}).get("tools_used") or [])
            if isinstance(t, dict)
            and str(t.get("tool") or "").lower().endswith("switch_workspace")]
        prev_switch = bool(switch_entries)
        vocative = (str(persona.get("name") or ""),)
        last_switch = next(
            (t for t in reversed(switch_entries)
             if isinstance(t.get("args"), dict) and t["args"].get("workspace")),
            None)
        ws_ask = (_intents.detect_workspace_switch_request(
            text, prev_switch, vocative) if timer is None else None)
        if (ws_ask is None and last_switch is not None and timer is None
                and _intents.detect_repeat_request(text)):
            prev_args = last_switch["args"]
            ws_ask = {"workspace": str(prev_args.get("workspace")),
                      "monitor": str(prev_args.get("monitor") or "") or None}
            log.info("[turn] workspace repeat persona=%s args=%s",
                     pid, ws_ask)
        ws_used: dict | None = None
        if ws_ask:
            ws_spec = next(
                (s for s in specs if s.available and s.source == "client"
                 and s.name.lower().endswith("__switch_workspace")), None)
            if ws_spec is not None:
                ws_args = {"workspace": ws_ask["workspace"]}
                if ws_ask["monitor"]:
                    ws_args["monitor"] = ws_ask["monitor"]
                label = f"switching to workspace {ws_ask['workspace']}"
                if ws_ask["monitor"]:
                    label += f" on the {ws_ask['monitor']} screen"
                call_id = f"hub-prefetch-{int(time.time() * 1000) % 1000000}"
                events.publish("tool_started", call_id=call_id,
                               tool=ws_spec.name, label=label)
                t0 = time.monotonic()
                # same wait semantics as the tool loop's client dispatch
                ws_deadline = (time.monotonic()
                               + float(tools_cfg.get("max_seconds") or 120))
                out_ws = clients_hub.dispatch(
                    ws_spec, ws_args, needs_approval=False, summary=label,
                    wait_seconds=max(5.0, ws_deadline - time.monotonic()))
                ms = int((time.monotonic() - t0) * 1000)
                ws_ok = bool(out_ws.get("ok"))
                ws_error = "" if ws_ok else str(
                    out_ws.get("error") or "client tool failed")
                events.publish("tool_finished", call_id=call_id,
                               tool=ws_spec.name, ok=ws_ok, ms=ms)
                store.log_tool(persona=pid, tool=ws_spec.name,
                               source="client",
                               arguments=json.dumps(ws_args), ok=ws_ok,
                               error=ws_error, ms=ms,
                               approval="auto (hub pre-act)")
                # args ride on the trace entry so a later "try that again"
                # can replay the exact same switch
                ws_used = {"tool": ws_spec.name, "label": label,
                           "source": "client", "auto": True,
                           "args": dict(ws_args)}
                system += "\n\n" + _intents.workspace_switch_block(
                    ws_ask["workspace"], ws_ask["monitor"],
                    payload=out_ws.get("result"), error=ws_error)
                log.info("[turn] workspace pre-act persona=%s target=%s "
                         "ok=%s ms=%d", pid, ws_ask["workspace"], ws_ok, ms)

        # workspace complaint read (docs/API.md "Workspace pre-act"): the
        # last turn switched a workspace and the owner says it did not show
        # ("Unfortunately, it isn't."). READ the live desktop state through
        # the client-tool channel and inject it — she reports what the read
        # says instead of insisting or re-switching behind his back.
        state_used: dict | None = None
        if (ws_ask is None and prev_switch and timer is None
                and _intents.detect_workspace_complaint(text)):
            state_spec = None
            for suffix in ("__desktop_monitors", "__desktop_focused"):
                state_spec = next(
                    (s for s in specs if s.available and s.source == "client"
                     and s.name.lower().endswith(suffix)), None)
                if state_spec is not None:
                    break
            if state_spec is not None:
                label = "checking the desktop state"
                call_id = f"hub-prefetch-{int(time.time() * 1000) % 1000000}"
                events.publish("tool_started", call_id=call_id,
                               tool=state_spec.name, label=label)
                t0 = time.monotonic()
                st_deadline = (time.monotonic()
                               + float(tools_cfg.get("max_seconds") or 120))
                out_state = clients_hub.dispatch(
                    state_spec, {}, needs_approval=False, summary=label,
                    wait_seconds=max(5.0, st_deadline - time.monotonic()))
                ms = int((time.monotonic() - t0) * 1000)
                st_ok = bool(out_state.get("ok"))
                st_error = "" if st_ok else str(
                    out_state.get("error") or "client tool failed")
                events.publish("tool_finished", call_id=call_id,
                               tool=state_spec.name, ok=st_ok, ms=ms)
                store.log_tool(persona=pid, tool=state_spec.name,
                               source="client", arguments="{}", ok=st_ok,
                               error=st_error, ms=ms,
                               approval="auto (hub pre-act)")
                state_used = {"tool": state_spec.name, "label": label,
                              "source": "client", "auto": True, "args": {}}
                system += "\n\n" + _intents.workspace_state_block(
                    out_state.get("result"), error=st_error)
                log.info("[turn] workspace state read persona=%s tool=%s "
                         "ok=%s ms=%d", pid, state_spec.name, st_ok, ms)

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

        # promised-action guard (docs/API.md "Promised-action guard"): the
        # turn made no tool call while tools were offered, yet the reply
        # announces one ("Let me check…"). Re-run the model ONCE this turn
        # with a nudge and keep that answer; the announced-only reply is
        # discarded (never stored, never spoken). Cap: one retry per turn.
        # The claimed-action twin (docs/API.md "Claimed-action guard"): an
        # imperative action request ("Try to switch to 4.") answered with a
        # completion claim ("Done — you're on workspace 4 now") and no tool
        # call — same retry machinery, different nudge, same shared cap.
        # Skipped on timer-fired turns (their "user" message is the timer
        # prompt, and the deterministic timer fallback already covers it).
        guard_nudge = ""
        guard_why = ""
        if (not out["tools_used"] and not ws_used and not state_used
                and offered_names and not out.get("tools_rejected")):
            if _intents.promises_action(out["text"]):
                guard_nudge, guard_why = (
                    _intents.PROMISE_NUDGE,
                    "promised action without a tool call")
            elif (timer is None
                    and _intents.detect_action_request(text, vocative)
                    and _intents.claims_completed_action(out["text"])):
                guard_nudge, guard_why = (
                    _intents.CLAIM_NUDGE,
                    "claimed a completed action without a tool call")
            # refusal twin (docs/API.md "Claimed-action guard"): she
            # REFUSED a request a matching tool could serve ("I can't",
            # "no way to", "press Super+4") instead of calling the tool.
            elif (timer is None
                    and _intents.refuses_tool_backed_action(out["text"])
                    and refusal_tool_offered(
                        _intents.detect_action_request_kind(text, vocative),
                        offered_names)):
                guard_nudge, guard_why = (
                    _intents.REFUSAL_NUDGE,
                    "refused a request a tool could serve")
            # hallucinated-tool-talk twin (docs/API.md "Claimed-action
            # guard"): the reply CITES tool evidence ("my tools insist…")
            # the turn never got — call the tool or say you don't know.
            elif _intents.claims_tool_evidence(out["text"]):
                guard_nudge, guard_why = (
                    _intents.TOOL_TALK_NUDGE,
                    "cited tool results without a tool call")
            # invent twin (docs/SELF_ANIMATION.md): "make up a dance"
            # answered in words while create_animation sat offered.
            elif (timer is None and "create_animation" in offered_names
                  and _animations.detect_invent_request(text)):
                guard_nudge, guard_why = (
                    _intents.INVENT_NUDGE,
                    "invent request without create_animation")
        if guard_nudge:
            log.info("[turn] nudged: %s persona=%s", guard_why, pid)
            retry_msgs = messages + [
                {"role": "assistant", "content": out["text"]},
                {"role": "system", "content": guard_nudge}]
            try:
                retry_out = run_turn(brain, settings, tools_cfg, specs,
                                     retry_msgs, ctx)
            except Exception as e:
                log.info("[turn] nudge retry failed persona=%s: %s", pid, e)
                retry_out = None
            if retry_out and str(retry_out.get("text") or "").strip():
                out = retry_out

        # the emotion tag never reaches the stored reply or the TTS
        emotion, answer = _emotion.parse_emotion(out["text"])
        # body actions are parsed from the FINAL reply only (the tool loop
        # may have streamed several model turns)
        answer, actions, ignored = _body.parse_actions(
            answer, body_actions,
            custom_actions=[r["name"] for r in store.list_animations(
                pid, anim_sha(pid), status="kept")])
        pv = anim_capture.get("preview_action")
        if (pv and len(actions) < _body.MAX_ACTIONS
                and all(a.get("name") != pv["name"] for a in actions)):
            actions.insert(0, pv)   # the reply carries the preview play

        if ignored:
            log.info("ignored_actions persona=%s names=%s", pid, ",".join(ignored))
        # docs/BODY.md §3 asked-for fallback: tag emission is stochastic
        # (worse at high temperature), so an explicit request the reply
        # forgot is added anyway; the model's own matching tag is just
        # flagged. Only for embodied personas with a body that can play it
        # (empty set otherwise).
        actions = _body.apply_asked(
            text, actions, body_actions if persona.get("embodied", True) else [])
        # TTS floor: the store and the audio routes must never see an empty
        # reply — breeze-tts rejects an empty input (HTTP 400 'input is
        # required'). A cut-off stream can arrive as '' even after brain's
        # retry, and emotion/action stripping can empty a tag-only reply
        # ('[happy]' is all the model left). Fall back to the placeholder.
        if not answer.strip():
            log.info("empty reply persona=%s; storing the empty-reply placeholder", pid)
            answer = _brain.EMPTY_REPLY
        tools_used = out["tools_used"]
        if wx_used:
            # the hub's own pre-fetched weather counts like a tool call for
            # the chip and the stored trace (auto: nobody had to approve it)
            tools_used.insert(0, wx_used)
        if ws_used:
            # same for the hub's own pre-acted workspace switch; last insert
            # wins position 0, so the switch leads the chip when both ran
            tools_used.insert(0, ws_used)
        if state_used:
            tools_used.insert(0, state_used)

        # asked-for fallback (docs/TIMERS.md "Reviewer additions"): she
        # answered "remind me in 20 seconds…" as if the timer existed but
        # never called the tool — the hub creates the timer the user
        # actually asked for. Skipped when the turn made one itself, when
        # the reply clearly refused, on timer-fired turns, and when the
        # `timer` tool was not offered this turn (tools off / permission
        # never — the hub never schedules behind her back). The reply text
        # may already state the time; no text rewrite.
        if (asked_timer and timer is None and not turn_created
                and "timer" in offered_names
                and not _timers.reply_refuses(answer)):
            try:
                turn_created.append(create_timer(
                    persona_id=pid, prompt=asked_timer["prompt"],
                    time_raw=asked_timer["time"], label=asked_timer["label"],
                    cancel_on=[], snapshot=timer_capture, asked=True))
                log.info("timer fallback persona=%s time=%s label=%s",
                         pid, asked_timer["time"], asked_timer["label"])
            except ValueError as exc:
                log.info("timer fallback skipped persona=%s: %s", pid, exc)
        timers_set = [{"id": r["id"], "label": r["label"],
                       "due_at_local": r.get("due_at_local", ""),
                       "asked": bool((r.get("captured") or {}).get("asked"))}
                      for r in turn_created]

        timer_id = timer["id"] if timer else None
        user_id = store.add_message(pid, "user", text, source, voice=voice_flag,
                                    timer_id=timer_id)
        # the spoken text is the cleaned reply (hub/speech.py): audio and any
        # client-side TTS use it, the bubble keeps all of it.
        spoken = _speech.speech_text(answer)
        if not spoken.strip():
            # nothing speakable survived the cleaner (a wordless reply):
            # never hand '' to TTS, speak the placeholder instead; the bubble
            # keeps what the model wrote (e.g. a bare link)
            spoken = _speech.speech_text(_brain.EMPTY_REPLY)
        assistant_id = store.add_message(pid, "assistant", answer, source,
                                         emotion=emotion, tools=tools_used,
                                         actions=actions, speech=spoken,
                                         memory_writes=memory_writes,
                                         timer_id=timer_id)
        bump_state()
        # The chat never waits for audio: audio_url points at the audio route,
        # which synthesises lazily (and caches on disk).
        audio_url = None
        if speak and tts_on() and (persona.get("voice") or {}).get("mode") != "off":
            audio_url = f"/api/messages/{assistant_id}/audio"
        finish = {"persona": pid, "message_id": assistant_id,
                  "emotion": emotion, "actions": actions}
        if timer:
            # docs/TIMERS.md: a fired timer's turn_finished carries the FULL
            # speak payload so clients can play it without another round-trip
            finish.update(origin="timer", persona_id=pid,
                          timer={"id": timer["id"], "label": timer["label"]},
                          reply=answer, speech=spoken, audio_url=audio_url)
        events.publish("turn_finished", **finish)
        data = {"reply": answer, "persona": pid, "switched": False,
                "emotion": emotion, "tools_used": tools_used,
                "actions": actions, "speech": spoken,
                "memory_writes": memory_writes,
                "timers_set": timers_set,
                "message_ids": [user_id, assistant_id],
                "state_version": store.version(),
                "audio_url": audio_url}
        if timer:
            data["timer"] = {"id": timer["id"], "label": timer["label"]}
        return ("ok", data)

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
        voice = bool(body.get("voice"))  # speech-recognised text (docs/API.md)
        norm = normalize_switch_text(text)

        def turn():  # blocking section: store + LLM, one turn per persona at a time
            target = match_switch(norm)
            if target:  # switch: activate, greet, store nothing
                # the switch line is an owner message in the chat it was
                # typed into — cancel_on chat.user_message timers honour it
                old_active = store.active()
                if old_active and old_active != target:
                    cancel_timers_on_event(old_active, "chat.user_message",
                                           "user_message")
                if store.set_active(target):
                    bump_state()
                persona = store.get_persona(target) or {}
                reply = persona.get("greeting") or f"{persona.get('name', target)} here."
                # docs/BODY.md: a switch greets with a wave when the online
                # body set can do it and the persona is embodied.
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
                               "speech": _speech.speech_text(reply),
                               "message_ids": [], "state_version": store.version(),
                               "timers_set": [], "audio_url": audio_url})

            pid = override or store.active()
            if not store.persona_exists(pid):
                return ("err", 404, f"no persona '{pid}'")
            # docs/TIMERS.md cancel hook: an owner message in this persona's
            # chat cancels their timers that asked to die on chat.user_message
            # (inside the lock, so it also catches timers claimed but not yet
            # run; a FIRED timer's own turn never runs this hook)
            with persona_lock_for(pid):
                cancel_timers_on_event(pid, "chat.user_message", "user_message")
                return run_chat_turn(pid, text, source, speak, voice)

        kind, *rest = await run_in_threadpool(turn)
        return rest[0] if kind == "ok" else err(rest[0], rest[1])

    # ── touch (docs/TOUCH.md) ───────────────────────────────────────────
    @app.post("/api/touch")
    async def post_touch(request: Request):
        """One physical touch from a body client. Fast no-LLM answers for
        mode off / cooldown / 10-min limit / busy / always-silent kinds;
        otherwise she gets ONE short in-character sentence (no tools) and
        the reply rides the same storage/audio path as a chat reply. A
        silently-ignored touch still lands as a grey system note plus a
        meta.silent assistant line — never a fake user message."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        kind_ = str(body.get("kind") or "")
        bad = _touch.validate(kind_)
        if bad:
            return err(400, bad)
        held = body.get("hold_ms", body.get("held_ms"))   # TOUCH.md: hold_ms
        held_ms = (float(held) if isinstance(held, (int, float))
                   and not isinstance(held, bool) else None)
        part = str(body.get("part") or "").strip() or None

        def work():
            pid = (str(body.get("persona") or "")
                   or str(body.get("persona_id") or "") or store.active())
            if not store.persona_exists(pid):
                return ("err", 404, f"no persona '{pid}'")
            cfg = touch_settings()
            mode = str(cfg.get("mode") or "quiet")

            def note_row(reason, spoken_row=None):
                """System note + the short silent assistant line (or the
                already-stored reaction), then bump. -> state_version."""
                store.add_message(pid, "system", _touch.note_text(kind_, mode),
                                  "touch", meta={
                                      "touch": {"kind": kind_, "mode": mode,
                                                "held_ms": held_ms, "part": part,
                                                "reason": reason, "note": True}})
                if spoken_row is None:
                    store.add_message(pid, "assistant", _touch.silent_line(reason),
                                      "touch", meta={
                                          "touch": {"kind": kind_, "mode": mode,
                                                    "silent": True,
                                                    "reason": reason}})
                bump_state()
                return store.version()

            lock = persona_lock_for(pid)
            have_lock = lock.acquire(blocking=False)
            try:
                spoken = store.spoken_touch_times(pid)
                now = datetime.now(timezone.utc)
                cutoff = (now - timedelta(seconds=_touch.RATE_WINDOW_S)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                reason = _touch.gate(
                    mode, kind_, held_ms,
                    cooldown_s=cfg.get("cooldown_s"),
                    last_spoken_iso=spoken[0] if spoken else None,
                    now_epoch=time.time(),
                    spoken_in_window=sum(1 for iso in spoken if iso >= cutoff),
                    max_per_10min=cfg.get("max_per_10min"),
                    busy=not have_lock)
                if reason != "llm":
                    return ("ok", _touch.touch_reply(
                        reason, note_row(reason), mode=mode))

                # ── the one-sentence reaction turn (persona lock held) ──
                persona = store.get_persona(pid)
                settings = llm_settings()
                touch_meta = {"kind": kind_, "mode": mode,
                              "held_ms": held_ms, "part": part}
                try:
                    messages = [{"role": "system",
                                 "content": persona["system_prompt"] + "\n\n"
                                 + _touch.system_line()}]
                    max_history = int(settings.get("max_history") or 40)
                    messages += [{"role": m["role"], "content": m["content"]}
                                 for m in store.recent_messages(pid, max_history)
                                 if str(m.get("content") or "").strip()]
                    messages.append({"role": "user",
                                     "content": _touch.reaction_prompt(
                                         kind_, held_ms, part)})
                    raw = brain.chat(settings, messages)
                except Exception as e:
                    log.info("[touch] LLM failed persona=%s kind=%s: %s",
                             pid, kind_, e)
                    return ("ok", _touch.touch_reply(
                        "llm_error", note_row("llm_error"), mode=mode))
                emotion, answer = _emotion.parse_emotion(raw)
                body_actions = (clients_hub.body_actions()
                                if persona.get("embodied", True) else [])
                answer, actions, ignored = _body.parse_actions(
                    answer, body_actions,
                    custom_actions=[r["name"] for r in store.list_animations(
                        pid, anim_sha(pid), status="kept")])
                if ignored:
                    log.info("[touch] ignored_actions persona=%s names=%s",
                             pid, ",".join(ignored))
                if not answer.strip():
                    answer = _brain.EMPTY_REPLY
                spoken_text = _speech.speech_text(answer)
                if not spoken_text.strip():
                    spoken_text = _speech.speech_text(_brain.EMPTY_REPLY)
                note_id = store.add_message(
                    pid, "system", _touch.note_text(kind_, mode), "touch",
                    meta={"touch": {**touch_meta, "reason": "llm", "note": True}})
                assistant_id = store.add_message(
                    pid, "assistant", answer, "touch", emotion=emotion,
                    actions=actions, speech=spoken_text,
                    meta={"touch": {**touch_meta, "note_id": note_id}})
                bump_state()
                audio_url = None
                if tts_on() and (persona.get("voice") or {}).get("mode") != "off":
                    audio_url = f"/api/messages/{assistant_id}/audio"
                if actions:
                    # the page plays the gesture even when the client's own
                    # mode makes audio local (docs/TOUCH.md: hub fires action,
                    # page plays it)
                    events.publish("action", persona_id=pid, emotion=emotion,
                                   actions=actions, touch=touch_meta)
                return ("ok", _touch.touch_reply(
                    "llm", store.version(), spoke=True, text=answer,
                    emotion=emotion, actions=actions, audio_url=audio_url,
                    mode=mode))
            finally:
                if have_lock:
                    lock.release()

        kind_, *rest = await run_in_threadpool(work)
        return rest[0] if kind_ == "ok" else err(rest[0], rest[1])

    # ── timer routes (docs/TIMERS.md H) ─────────────────────────────────
    def timer_public(row: dict) -> dict:
        """API view: no captured snapshot (its settings carry the api_key —
        write-only, like GET /api/settings never returns it). `asked` and
        `chain_depth` come out of the snapshot (docs/TIMERS.md additions)."""
        tz = effective_timezone()
        out = {k: v for k, v in row.items() if k != "captured"}
        cap = row.get("captured") or {}
        out["asked"] = bool(cap.get("asked"))
        out["chain_depth"] = int(cap.get("chain_depth") or 0)
        out["timezone"] = effective_timezone_name()
        try:
            due = datetime.strptime(row["due_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            out["due_at_local"] = _timers.to_local(due, tz)
        except ValueError:
            out["due_at_local"] = row["due_at"]
        return out

    @app.get("/api/timers")
    def get_timers(status: str = ""):
        rows = store.list_timers()
        if status:
            rows = [r for r in rows if r["status"] == status]
        return {"timers": [timer_public(r) for r in rows]}

    @app.delete("/api/timers/{timer_id}")
    def delete_timer(timer_id: int):
        row = store.get_timer(timer_id)
        if row is None:
            return err(404, f"no timer {timer_id}")
        done = store.cancel_timer_rows([timer_id])
        if not done:
            return err(409, f"timer {timer_id} is {row['status']}, not cancellable")
        _publish_cancelled(done[0], "request")
        bump_state()
        return {"cancelled": timer_id, "label": row["label"]}

    @app.post("/api/chat/read")
    async def post_chat_read(request: Request):
        """The client says the owner READ this persona's chat: cancels that
        persona's timers whose cancel_on names chat.read (docs/TIMERS.md)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        pid = str(body.get("persona_id") or "") or store.active()
        if not store.persona_exists(pid):
            return err(404, f"no persona '{pid}'")
        rows = cancel_timers_on_event(pid, "chat.read", "chat_read")
        return {"cancelled": [r["id"] for r in rows]}

    # ── custom animation routes (docs/SELF_ANIMATION.md) ─────────────────
    def anim_resolve_sha(persona_id: str, model: str) -> str:
        """`model` query: '' / 'current' → this persona's live avatar model;
        anything else is a literal model_sha256 (e.g. 'default')."""
        m = str(model or "").strip().lower()
        return anim_sha(persona_id) if m in ("", "current") else m

    @app.get("/api/personas/{persona_id}/animations")
    def get_animations(persona_id: str, model: str = "current",
                       status: str = "all"):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        if status not in ("all", "kept", "draft"):
            return err(422, "status must be all, kept or draft")
        if model not in ("", "current", "all"):
            sha = anim_resolve_sha(persona_id, model)
            rows = store.list_animations(persona_id, model_sha256=sha,
                                         status=None if status == "all" else status)
            return {"model_sha256": sha, "animations": rows}
        current = anim_sha(persona_id)
        rows = store.list_animations(
            persona_id,
            model_sha256=None if model == "all" else current,
            status=None if status == "all" else status)
        return {"model_sha256": current, "animations": rows}

    @app.post("/api/personas/{persona_id}/animations", status_code=201)
    async def post_animations(persona_id: str, request: Request):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        try:
            clean, notes = _animations.validate_animation(body)
        except ValueError as e:
            return err(422, str(e))
        sha = anim_sha(persona_id)
        status = "kept" if animations_settings().get("auto_keep") else "draft"
        row = store.add_animation(persona_id, sha, clean, status=status)
        if row is None:
            return err(409, f"an animation named '{clean['name']}' already "
                            "exists on this model")
        events.publish("animations_changed", persona_id=persona_id,
                       model_sha256=sha)
        return {"animation": row, "clamped": notes,
                "lint": _animations.lint_animation(body, clean)}

    @app.put("/api/personas/{persona_id}/animations/{name}")
    async def put_animation(persona_id: str, name: str, request: Request,
                            model: str = ""):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        unknown = [k for k in body if k not in ("name", "description", "status")]
        if unknown:
            return err(422, "only name, description and status are editable")
        sha = anim_resolve_sha(persona_id, model)
        if store.get_animation(persona_id, name, sha) is None:
            return err(404, f"no animation '{name}' on this model")
        new_name = body.get("name")
        if new_name is not None:
            if not isinstance(new_name, str) or \
                    not _animations.NAME_RE.match(new_name):
                return err(422, "name must match ^[a-z][a-z0-9_]{1,31}$")
            if new_name in _animations.reserved_names():
                return err(422, f"name '{new_name}' is reserved (built-in "
                                "gesture/action name)")
            new_name = None if new_name == name else new_name
        description = body.get("description")
        if description is not None:
            if not isinstance(description, str):
                return err(422, "description must be a string")
            description = description.strip()[:_animations.MAX_DESCRIPTION_CHARS]
        status = body.get("status")
        if status is not None and status not in ("kept", "draft"):
            return err(422, 'status must be "kept" or "draft"')
        row, cerr = store.update_animation(persona_id, name, sha,
                                           new_name=new_name,
                                           description=description,
                                           status=status)
        if cerr == "conflict":
            return err(409, f"an animation named '{body.get('name')}' "
                            "already exists on this model")
        if cerr == "missing" or row is None:
            return err(404, f"no animation '{name}' on this model")
        events.publish("animations_changed", persona_id=persona_id,
                       model_sha256=sha)
        return {"animation": row}

    @app.delete("/api/personas/{persona_id}/animations/{name}",
                status_code=204)
    def delete_animation_route(persona_id: str, name: str, model: str = ""):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        sha = anim_resolve_sha(persona_id, model)
        if store.delete_animation(persona_id, name, sha) is None:
            return err(404, f"no animation '{name}' on this model")
        events.publish("animations_changed", persona_id=persona_id,
                       model_sha256=sha)
        return Response(status_code=204)

    @app.post("/api/personas/{persona_id}/animations/{name}/preview")
    def preview_animation(persona_id: str, name: str, model: str = ""):
        """Publishes the play action through the same event channel chat
        replies/actions ride, so the page and desktop preview it right away."""
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        sha = anim_resolve_sha(persona_id, model)
        row = store.get_animation(persona_id, name, sha)
        if row is None:
            return err(404, f"no animation '{name}' on this model")
        action = {"name": name, "at": 0.0,
                  "type": "gesture", "custom": True, "preview": True}
        # carry the animation inline too (SELF_ANIMATION.md): the page may
        # not have refreshed its cache since this animation appeared
        inline = _anim_tools.inline_animation_doc({
            k: row[k] for k in ("name", "description", "duration_s", "loop",
                                "ease", "keyframes") if k in row})
        if inline is not None:
            action["animation"] = inline
        events.publish("action", persona_id=persona_id, emotion=None,
                       actions=[action])
        # honest delivery (the owner asked): the play only lands if a desktop
        # app is online to forward it. Still 200 — the event is published, and
        # any other live-events follower (Android) rides it too.
        online = any(c.get("kind") == "desktop"
                     for c in clients_hub.online_clients())
        out = {"ok": True, "delivered": online, "persona_id": persona_id,
               "name": name, "model_sha256": sha}
        if not online:
            out["reason"] = "no desktop online"
        return out



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
            if "units" in tools:
                units_val = str(tools["units"] or "").strip().lower()
                if not units_val:
                    saved_tools.pop("units", None)   # blank = back to the default
                elif units_val == "imperial":
                    saved_tools["units"] = "us"      # legacy alias (docs/API.md)
                elif units_val not in ("auto", "metric", "uk", "us"):
                    return err(422, "tools.units must be auto, metric, uk or us")
                else:
                    saved_tools["units"] = units_val
            for qty_key, allowed in (
                    ("units_temperature", ("celsius", "fahrenheit")),
                    ("units_wind", ("kmh", "mph", "ms", "kn")),
                    ("units_precipitation", ("mm", "inch"))):
                if qty_key in tools:
                    q_val = str(tools[qty_key] or "").strip().lower()
                    if not q_val:
                        saved_tools.pop(qty_key, None)  # blank = from the preset
                    elif q_val not in allowed:
                        return err(422, f"tools.{qty_key} must be one of "
                                        f"{' | '.join(allowed)} (or blank)")
                    else:
                        saved_tools[qty_key] = q_val
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
        if "memory" in body:
            mem = body["memory"]
            if not isinstance(mem, dict):
                return err(422, "memory must be an object")
            saved_mem = stored_memory()
            for key in ("enabled", "auto_remember"):
                if key in mem:
                    if not isinstance(mem[key], bool):
                        return err(422, f"memory.{key} must be a boolean")
                    saved_mem[key] = mem[key]
            if "prompt_budget_tokens" in mem:
                raw = mem["prompt_budget_tokens"]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                        or not 100 <= int(raw) <= 4000:
                    return err(422, "memory.prompt_budget_tokens must be "
                                    "an integer between 100 and 4000")
                saved_mem["prompt_budget_tokens"] = int(raw)
            store.set_json("memory", saved_mem)
        if "timers" in body:
            tmr = body["timers"]
            if not isinstance(tmr, dict):
                return err(422, "timers must be an object")
            saved_tmr = stored_timers()
            if "allow_chaining" in tmr:
                if not isinstance(tmr["allow_chaining"], bool):
                    return err(422, "timers.allow_chaining must be a boolean")
                saved_tmr["allow_chaining"] = tmr["allow_chaining"]
            store.set_json("timers_settings", saved_tmr)
        if "touch" in body:
            tch = body["touch"]
            if not isinstance(tch, dict):
                return err(422, "touch must be an object")
            saved_tch = stored_touch()
            if "mode" in tch:
                if tch["mode"] not in _touch.MODES:
                    return err(422, "touch.mode must be one of off, quiet, chatty")
                saved_tch["mode"] = tch["mode"]
            if "cooldown_s" in tch:
                raw = tch["cooldown_s"]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                        or not 0 <= float(raw) <= 3600:
                    return err(422, "touch.cooldown_s must be a number "
                                    "between 0 and 3600")
                saved_tch["cooldown_s"] = float(raw)
            if "max_per_10min" in tch:
                raw = tch["max_per_10min"]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                        or not 0 <= int(raw) <= 100:
                    return err(422, "touch.max_per_10min must be an integer "
                                    "between 0 and 100")
                saved_tch["max_per_10min"] = int(raw)
            store.set_json("touch", saved_tch)
        if "animations" in body:
            an = body["animations"]
            if not isinstance(an, dict):
                return err(422, "animations must be an object")
            saved_an = stored_animations()
            if "auto_keep" in an:
                if not isinstance(an["auto_keep"], bool):
                    return err(422, "animations.auto_keep must be a boolean")
                saved_an["auto_keep"] = an["auto_keep"]
            store.set_json("animations_settings", saved_an)
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
                    # sync the EFFECTIVE list: an env-defaulted Home Assistant
                    # server stays when the saved list does not name one
                    mcp_manager.sync(mcp_servers())
                except Exception as e:  # never fail the save on the reconnect
                    events.publish("mcp_error", error=f"{type(e).__name__}: {e}"[:300])
        if "timezone" in body:
            # docs/TIMERS.md H: the zone timer times and spoken times use.
            # blank = unset → the server TZ (compose sets TZ).
            tz_val = str(body["timezone"] or "").strip()
            if not tz_val:
                store.del_json("timezone")
            elif not _timers.valid_timezone(tz_val):
                return err(422, f"'{tz_val}' is not a valid IANA timezone "
                                "(e.g. Europe/London)")
            else:
                store.set_json("timezone", tz_val)
        if "location" in body:
            # docs/API.md "Weather": the resolved place the web page picked
            # from GET /api/geocode. null / {} / blank name = unset → the
            # RIKO_LOCATION env default (or no location at all).
            loc = body["location"]
            if loc is None or loc == {}:
                store.del_json("location")
            elif not isinstance(loc, dict):
                return err(422, "location must be an object or null")
            else:
                saved_loc = stored_location()
                name = str(loc.get("name") or "").strip()
                if "name" in loc and not name:
                    for k in LOCATION_PLACE_KEYS:
                        saved_loc.pop(k, None)   # blank = env default / none
                elif any(k in loc for k in ("name", "latitude", "longitude")):
                    # a full resolved pick replaces the whole place block
                    try:
                        lat = float(str(loc.get("latitude")))
                        lon = float(str(loc.get("longitude")))
                    except (TypeError, ValueError):
                        return err(422, "location.latitude/longitude must be "
                                        "numbers (pick a match from the search)")
                    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                        return err(422, "location latitude/longitude out of range")
                    if not name:
                        return err(422, "location.name is required with a place")
                    new_loc = {"name": name[:200], "latitude": lat, "longitude": lon}
                    for k in ("timezone", "country", "country_code", "admin1"):
                        new_loc[k] = str(loc.get(k) or "").strip()[:100]
                    saved_loc = {**saved_loc, **new_loc}
                store.set_json("location", saved_loc)
        if "spotify" in body:
            # music phase 1 (docs/SPOTIFY_NOTES.md): schema keys only. The
            # tokens are NEVER written through here (they live in
            # spotify_tokens, written by the connect flow), and an empty
            # client_id NEVER wipes the saved one — api_key semantics.
            sp = body["spotify"]
            if not isinstance(sp, dict):
                return err(422, "spotify must be an object")
            saved_sp = stored_spotify()
            if str(sp.get("client_id") or "").strip():
                saved_sp["client_id"] = str(sp["client_id"]).strip()[:120]
            if sp.get("clear_client_id"):
                saved_sp.pop("client_id", None)
            if "device_name" in sp:
                name = str(sp["device_name"] or "").strip()
                if name:
                    saved_sp["device_name"] = name[:60]
                else:
                    saved_sp.pop("device_name", None)   # blank = 'Riko'
            for key in ("accounts_url", "api_url"):
                if key in sp:
                    value = str(sp[key] or "").strip()
                    if value:
                        if not value.startswith(("http://", "https://")):
                            return err(422, f"spotify.{key} must be an "
                                            "http(s) URL")
                        saved_sp[key] = value[:300]
                    else:
                        saved_sp.pop(key, None)  # blank = built-in default
            if "enabled" in sp:
                if not isinstance(sp["enabled"], bool):
                    return err(422, "spotify.enabled must be a boolean")
                saved_sp["enabled"] = sp["enabled"]
            if "idle_poll_s" in sp:
                # slow now_playing watch (docs/SPOTIFY_NOTES.md): seconds;
                # 0 disables; the hub floors anything else at 15 s itself
                v = sp["idle_poll_s"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or v != v or v < 0 or v > 86400:
                    return err(422, "spotify.idle_poll_s must be a number "
                                    "of seconds >= 0 (0 disables the "
                                    "idle watch)")
                saved_sp["idle_poll_s"] = float(v)
            store.set_json("spotify", saved_sp)
            if sp.get("enabled"):
                spotify_hub.wake()   # re-check playback right after enabling
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
        if body.get("memory"):
            changed |= store.del_json("memory")
        if body.get("timers"):
            changed |= store.del_json("timers_settings")
        if body.get("touch"):
            changed |= store.del_json("touch")
        if body.get("animations"):
            changed |= store.del_json("animations_settings")
        if body.get("mcp"):
            changed |= store.del_json("mcp")
            mcp_manager.sync(mcp_servers())
        if body.get("switch_phrases"):
            changed |= store.del_json("switch_phrases")
        if body.get("timezone"):
            changed |= store.del_json("timezone")
        if body.get("location"):
            changed |= store.del_json("location")
        if body.get("spotify"):
            changed |= store.del_json("spotify")   # tokens survive a reset
        if not (body.get("llm") or body.get("tts") or body.get("tools")
                or body.get("memory") or body.get("mcp") or body.get("timers")
                or body.get("switch_phrases") or body.get("timezone")
                or body.get("location") or body.get("spotify")
                or body.get("touch") or body.get("animations")):
            return err(422, 'set "llm", "tts", "tools", "memory", "mcp", '
                            '"timers", "touch", "animations", '
                            '"switch_phrases", "timezone", '
                            '"location" and/or "spotify" to true')
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
        # audio speaks the CLEANED text (no URLs/markdown, every prose
        # paragraph, details blocks dropped — hub/speech.py); always
        # recomputed so rule upgrades beat stale rows.
        # -r<N> in the name is the rules version: old clips never survive it.
        return serve_cached_audio(
            f"{message_id}-v{persona['voice_version']}"
            f"-r{_speech.SPEECH_RULES_VERSION}{suffix}.wav",
            persona, _speech.speech_text(message["content"]),
            None if not emotion or emotion == "neutral" else emotion)

    @app.get("/api/personas/{persona_id}/greeting/audio")
    def get_greeting_audio(persona_id: str, request: Request):
        persona = persona_or_404(persona_id)
        if not persona:
            return err(404, f"no persona '{persona_id}'")
        # ?v= is informational (clients cache by it); the on-disk key is server-side
        return serve_cached_audio(
            f"greeting-{persona_id}-v{persona['voice_version']}"
            f"-r{_speech.SPEECH_RULES_VERSION}.wav",
            persona, _speech.speech_text(default_speech_text(persona)))

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
        # same spoken-text rules as chat audio: a client that posts a raw
        # reply (markdown and all) still hears clean speech
        text = _speech.speech_text(text)
        if not text:
            return err(422, "nothing speakable in text")
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
                                                       permissions, "",
                                                       s.permission_default),
                          "client_id": s.client_id})
        for s in mcp_manager.tool_info():
            tools.append({**s,
                          "permission": permission_for(s["name"], "mcp", s["risk"],
                                                       permissions,
                                                       s["permission_override"],
                                                       s.get("permission_default",
                                                            ""))})
        # music phase 1: the spotify_* rows are always listed (permissions
        # stay editable while Spotify is off), availability = configured
        for s in build_spotify_specs(spotify_hub, cfg):
            tools.append({"name": s.name, "display": s.display, "source": "hub",
                          "risk": s.risk, "available": s.available,
                          "description": s.description,
                          "permission": permission_for(s.name, "hub", s.risk,
                                                       permissions)})
        return {"tools": tools, "settings": cfg, "defaults": default_tools(),
                "status": tools_status_response(),
                # online body set (docs/BODY.md §1): what she can do right now
                "body_actions": clients_hub.body_actions()}

    @app.get("/api/geocode")
    def get_geocode(request: Request):
        """Location picker proxy for the Settings page (docs/API.md
        "Weather"): Open-Meteo geocoding, up to 5 matches, best first.
        5 s timeout; nothing is cached at the route beyond the weather
        module's shared 10-min geocode cache."""
        q = str(request.query_params.get("q") or "").strip()
        if not q:
            return err(422, "q (a place name) is required")
        try:
            return {"results": _weather.geocode(q)}
        except _weather.WeatherError as e:
            return err(502, str(e))

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

    # ── spotify (music phase 1, docs/SPOTIFY_NOTES.md) ─────────────────
    # The hub owns the ONE Spotify connection. Auth is Authorization Code
    # + PKCE (client id only, no secret); the redirect lands on a loopback
    # listener (the desktop app on http://127.0.0.1:8911/callback) or the
    # owner pastes the redirected URL back on the Settings page. Tokens
    # live in the store (write-only); nothing here streams audio — the
    # Player API only steers Spotify Connect devices.
    @app.get("/api/spotify/status")
    def get_spotify_status():
        """{enabled, configured, connected, connected_as, scope, device_name,
        token_expires_at, quota_backoff_left, now_playing, polling, ...} —
        booleans/expiry only, never a token."""
        return spotify_hub.status()

    @app.get("/api/spotify/connect-url")
    def get_spotify_connect_url(redirect_uri: str = ""):
        """Start the handshake: {url, state, code_verifier, redirect_uri}.
        The desktop listener and the Settings page both fetch this; the hub
        keeps the PKCE pair until connect() consumes it (single use)."""
        try:
            return spotify_hub.connect_url(redirect_uri)
        except _spotify.SpotifyError as e:
            return spotify_error_response(e)

    @app.post("/api/spotify/connect")
    async def post_spotify_connect(request: Request):
        """Finish the handshake: {code, redirect_uri, code_verifier?} from
        the desktop listener, or {paste: <the full redirected URL>} from the
        Settings page. code_verifier falls back to the stored handshake."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        try:
            return await run_in_threadpool(spotify_hub.connect, body)
        except _spotify.SpotifyError as e:
            return spotify_error_response(e)

    @app.post("/api/spotify/disconnect")
    def post_spotify_disconnect():
        """Drop the tokens (settings + app stay; Connect again to re-link)."""
        return spotify_hub.disconnect()

    @app.get("/api/spotify/now-playing")
    async def get_spotify_now_playing():
        """Late-client answer to 'what is playing': the cached young snapshot
        or one fresh API call. 409 while not connected, 429 inside a quota
        back-off window."""
        try:
            return await run_in_threadpool(spotify_hub.now_playing)
        except _spotify.SpotifyError as e:
            return spotify_error_response(e)

    @app.post("/api/spotify/wake")
    def post_spotify_wake():
        """Desktop fast-path nudge (docs/SPOTIFY_NOTES.md): the desktop's
        Soloist listener just saw a local playback event, so start the
        FAST now_playing poll immediately instead of waiting for the idle
        watch. Harmless when idle/disconnected — poll_tick clears the flag
        again on the first not-playing answer; auth is the hub's usual
        api_key."""
        spotify_hub.wake()
        return {"ok": True, "polling": True}

    @app.get("/api/spotify/devices")
    async def get_spotify_devices():
        """[{id, name, type, active, volume}] — cached 60 s (quota
        discipline, docs/SPOTIFY_NOTES.md)."""
        try:
            return {"devices": await run_in_threadpool(spotify_hub.devices)}
        except _spotify.SpotifyError as e:
            return spotify_error_response(e)

    @app.post("/api/spotify/transfer")
    async def post_spotify_transfer(request: Request):
        """{device: name-or-id}: make that device the player now
        ('make this the player' on the Settings card)."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        try:
            return await run_in_threadpool(
                spotify_hub.transfer, str(body.get("device") or ""))
        except _spotify.SpotifyError as e:
            return spotify_error_response(e)

    @app.post("/api/spotify/control")
    async def post_spotify_control(request: Request):
        """{action: play|pause|toggle|next|previous}: transport control for
        the avatar's song ticker (docs/SPOTIFY_NOTES.md). Answers with the
        fresh now-playing snapshot so the optimistic page UI can reconcile;
        409 while not connected, 429 inside a quota back-off."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        action = str(body.get("action") or "").strip().lower()
        if action not in ("play", "pause", "toggle", "next", "previous"):
            return err(422, "action must be play|pause|toggle|next|previous")
        try:
            done = await run_in_threadpool(spotify_hub.control, action)
            snap = await run_in_threadpool(spotify_hub.now_playing, True)
            return {"ok": True, "action": done.get("action"),
                    "now_playing": snap}
        except _spotify.SpotifyError as e:
            return spotify_error_response(e)

    # ── long-term memory (docs/WALK_MEMORY.md M) ────────────────────────
    def valid_fact_scope(scope: str) -> bool:
        return scope == "user" or (
            scope.startswith("persona:")
            and store.persona_exists(scope[len("persona:"):]))

    @app.get("/api/memory")
    def get_memory(scope: str = "", q: str = "", limit: int = 200, offset: int = 0):
        """Facts newest-first, or FTS-ranked when q is set. scope filters to
        'user' or one persona's notes; blank = everything."""
        limit = max(1, min(500, limit))
        offset = max(0, offset)
        scopes = None
        if scope:
            if not valid_fact_scope(scope):
                return err(422, "scope must be 'user' or 'persona:<id>' of "
                                "an existing persona")
            scopes = [scope]
        if q.strip():
            facts = store.search_facts(q, scopes=scopes, limit=limit)
            return {"facts": facts, "total": len(facts), "query": q.strip()}
        facts, total = store.list_facts(scopes=scopes, limit=limit, offset=offset)
        return {"facts": facts, "total": total}

    @app.get("/api/memory/export")
    def get_memory_export():
        return {"version": 1, "exported_at": now_iso(),
                "facts": store.export_facts()}

    @app.post("/api/memory/import")
    async def post_memory_import(request: Request):
        """Feed facts back in (the export shape works as-is). Body
        {facts: [{text, scope?, pinned?}], replace?: false}. New ids are
        assigned; unknown persona scopes and empty texts reject the whole
        import (all-or-nothing beats a half-restored memory)."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict) or not isinstance(body.get("facts"), list):
            return err(422, 'body must be {"facts": [...]}')
        items = body["facts"][:5000]
        cleaned = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                return err(422, f"facts[{i}] must be an object")
            text = _memory.clamp_fact_text(item.get("text"))
            if not text:
                return err(422, f"facts[{i}].text is required")
            scope = str(item.get("scope") or "user")
            if not valid_fact_scope(scope):
                return err(422, f"facts[{i}].scope '{scope}' is not 'user' or "
                                "an existing 'persona:<id>'")
            cleaned.append((text, scope, bool(item.get("pinned"))))
        deleted = 0
        if body.get("replace"):
            deleted = len(store.delete_all_facts())
        imported = [store.add_fact(t, scope=s, pinned=p)
                    for (t, s, p) in cleaned]
        for f in imported:
            events.publish("memory_changed", id=f["id"], op="added")
        bump_state()
        return {"imported": len(imported), "replaced": deleted}

    @app.post("/api/memory", status_code=201)
    async def post_memory(request: Request):
        """Add a fact from the page: {text, scope?, pinned?}. Near-identical
        text in the same scope updates the existing fact (same dedup as the
        remember tool), so {"updated": true} can come back with 201."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        text = _memory.clamp_fact_text(body.get("text"))
        if not text:
            return err(422, "text is required")
        scope = str(body.get("scope") or "user")
        if not valid_fact_scope(scope):
            return err(422, "scope must be 'user' or 'persona:<id>' of an "
                            "existing persona")
        fact, updated = store.upsert_fact(text, scope=scope,
                                          pinned=bool(body.get("pinned")))
        events.publish("memory_changed", id=fact["id"],
                       op="updated" if updated else "added")
        bump_state()
        return {**fact, "updated": updated}

    @app.put("/api/memory/{fact_id}")
    async def put_memory(fact_id: int, request: Request):
        """Edit a fact: {text?, pinned?} (inline edit / pin toggle)."""
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        text, pinned = None, None
        if "text" in body:
            text = _memory.clamp_fact_text(body.get("text"))
            if not text:
                return err(422, "text must not be empty")
        if "pinned" in body:
            if not isinstance(body["pinned"], bool):
                return err(422, "pinned must be a boolean")
            pinned = body["pinned"]
        if text is None and pinned is None:
            return err(422, "nothing to change: send text and/or pinned")
        fact = store.update_fact(fact_id, text=text, pinned=pinned)
        if not fact:
            return err(404, f"no memory fact {fact_id}")
        events.publish("memory_changed", id=fact_id, op="updated")
        bump_state()
        return fact

    @app.delete("/api/memory/{fact_id}", status_code=204)
    def delete_memory(fact_id: int):
        if not store.delete_fact(fact_id):
            return err(404, f"no memory fact {fact_id}")
        events.publish("memory_changed", id=fact_id, op="deleted")
        bump_state()
        return Response(status_code=204)

    @app.delete("/api/memory")
    def delete_memory_all(scope: str = "all", confirm: str = ""):
        """Forget everything. Needs confirm=yes (the page makes the user type
        the word); scope selects 'all', 'user' or one persona's notes."""
        if confirm != "yes":
            return err(400, "confirm=yes required")
        if scope == "all":
            ids = store.delete_all_facts()
        elif valid_fact_scope(scope):
            ids = store.delete_facts_by_scope(scope)
        elif scope.startswith("persona:"):
            return err(404, f"no persona '{scope[len('persona:'):]}'")
        else:
            return err(422, "scope must be 'all', 'user' or 'persona:<id>'")
        for fid in ids:
            events.publish("memory_changed", id=fid, op="deleted")
        bump_state()
        return {"deleted": len(ids)}

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
    app.state.persona_lock_for = persona_lock_for  # tests: hold it to force reason=busy

    # MCP servers from a previous run connect in the background as soon as
    # the app exists (works in local mode too, where the parent app may not
    # forward lifespan events); clean shutdown closes the sessions.
    try:
        mcp_manager.sync(mcp_servers())
    except Exception as e:
        events.publish("mcp_error", error=f"startup sync failed: "
                                          f"{type(e).__name__}: {e}"[:300])

    # ── timer scheduler (docs/TIMERS.md H) ──────────────────────────────
    # Daemon thread, claims due timers every poll_s and fires them as chat
    # turns under the persona lock (never-interrupt). start() first resets
    # firing->pending: a timer claimed right before a crash fires late.
    timer_poll_s = max(1.0, float(timers_defaults.get("poll_s", 1.0)))
    timer_retry_s = max(1.0, float(timers_defaults.get("retry_delay_s", 30.0)))
    timer_scheduler = TimerScheduler(store, events, fire_timer_turn,
                                     persona_lock_for,
                                     poll_s=timer_poll_s,
                                     retry_delay_s=timer_retry_s)
    app.state.timer_scheduler = timer_scheduler
    # The thread only runs for a real hub (main() passes poll_s explicitly) —
    # test apps built via create_app() drive scheduler.tick() directly, so
    # per-test daemon threads never pile up. A test that wants the live loop
    # passes timers_defaults={"poll_s": ...} or {"start": True}.
    if timers_defaults.get("start") or timers_defaults.get("poll_s") is not None:
        timer_scheduler.start()

    # ── spotify now_playing poller (music phase 1) ──────────────────────
    # Daemon thread like the timer scheduler: FAST polls currently-playing
    # ≤ every 5 s WHILE PLAYING, keeps the slow interest-gated idle watch
    # (spotify.idle_poll_s, default 30 s) while NOT playing so external
    # play/resume is noticed, takes one snapshot at start (correct state
    # after a restart), and publishes now_playing only on state changes.
    # main() passes start=True; tests pass poll_s (the thread runs but
    # stays interest-gated, so their manual poll_tick()/idle_tick() drives
    # the cadence without competing API calls).
    app.state.spotify = spotify_hub
    if spotify_defaults.get("start") or spotify_defaults.get("poll_s") is not None:
        spotify_hub.start()

    @app.on_event("shutdown")
    async def _mcp_shutdown():
        await run_in_threadpool(mcp_manager.shutdown)

    @app.on_event("shutdown")
    async def _timers_shutdown():
        timer_scheduler.stop()

    @app.on_event("shutdown")
    async def _spotify_shutdown():
        spotify_hub.stop()

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
        # weather unit preset: auto|metric|uk|us (legacy imperial -> us)
        "units": _weather.normalize_preset(os.getenv("RIKO_UNITS", "")),
    }
    # Location (docs/API.md "Weather"): RIKO_LOCATION is a plain place name
    # ("Gloucester, UK" or a postcode-ish town) used until the owner picks a
    # place on the Settings page; geocoded lazily at first use (cached).
    location_defaults = {"place": os.getenv("RIKO_LOCATION", "")}
    # Music phase 1 (docs/SPOTIFY_NOTES.md): RIKO_SPOTIFY_ACCOUNTS_URL /
    # RIKO_SPOTIFY_API_URL override the Spotify hosts (tests point them at
    # the in-repo fake server); the client id itself is Settings-page-only.
    # start=True: the real hub runs the now_playing poller thread.
    spotify_defaults = {
        "accounts_url": os.getenv("RIKO_SPOTIFY_ACCOUNTS_URL", ""),
        "api_url": os.getenv("RIKO_SPOTIFY_API_URL", ""),
        "start": True,
    }
    # Home Assistant (docs/API.md "Home Assistant via MCP"): RIKO_HA_URL is
    # the HA base URL, RIKO_HA_TOKEN the long-lived access token; both set =
    # the built-in 'homeassistant' MCP server is offered. The token never
    # enters the database — the server config only carries ${RIKO_HA_TOKEN}.
    mcp_defaults = {
        "ha_url": os.getenv("RIKO_HA_URL", ""),
        "ha_token": os.getenv("RIKO_HA_TOKEN", ""),
    }
    app = create_app(data_dir, api_key=os.getenv("RIKO_HUB_KEY") or None,
                     llm_defaults=llm_defaults, tts_defaults=tts_defaults,
                     tools_defaults=tools_defaults, mcp_defaults=mcp_defaults,
                     location_defaults=location_defaults,
                     timers_defaults={"poll_s": os.getenv("RIKO_TIMER_POLL_S", "1"),
                                      "allow_chaining": os.getenv(
                                          "RIKO_TIMER_ALLOW_CHAINING", "")
                                      .strip().lower() in ("1", "true", "yes", "on")},
                     spotify_defaults=spotify_defaults)
    host = os.getenv("RIKO_HUB_HOST", "0.0.0.0")
    port = int(os.getenv("RIKO_HUB_PORT", "8002"))
    print(f"[hub] data={data_dir} llm={llm_defaults['base_url'] or '(not configured)'} "
          f"tts={tts_defaults['base_url'] or '(off)'} "
          f"searxng={tools_defaults['searxng_url'] or '(off)'} "
          f"homeassistant={'on' if mcp_defaults['ha_url'] and mcp_defaults['ha_token'] else '(off)'} "
          f"mcp={'on' if mcp_available() else 'unavailable: mcp package not installed'}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
