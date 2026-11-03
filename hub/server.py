"""The Riko hub server: personas, chat memory, settings and the web config page.

Two ways to run it, same code path:
  * local mode — `from hub.server import create_app` and mount in-process
  * standalone — `cd hub && python server.py` (what the Docker image does)

API contract: docs/DESIGN.md at the repo root; full reference in docs/API.md.
"""
import hmac
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # `python server.py` inside hub/ must find its siblings
    sys.path.insert(0, str(_HERE))

import threading

from fastapi import FastAPI, File, Request, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402

try:  # package import (repo root: `from hub.server import create_app`)
    from . import brain as _brain
    from .store import Store, now_iso
except ImportError:  # standalone: modules sit next to this file (Docker: /app)
    import brain as _brain  # type: ignore[no-redef]
    from store import Store, now_iso  # type: ignore[no-redef]

HUB_VERSION = "1"
STATIC_DIR = _HERE / "static"
DEFAULT_AVATAR = _HERE / "assets" / "default_avatar.vrm"
MAX_AVATAR_BYTES = 200 * 1024 * 1024  # 200 MB
GLTF_MAGIC = b"glTF"

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

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG.sub("-", str(name).strip().lower()).strip("-")


def normalize_switch_text(text: str) -> str:
    """Lower-case, trim, strip trailing . ! ? , — the form switch commands are matched in."""
    return str(text).strip().lower().rstrip(".!?,")


def create_app(data_dir, api_key: str | None = None,
               llm_defaults: dict | None = None) -> FastAPI:
    """Build the hub app over `data_dir` (SQLite + avatars live there).

    api_key None/empty disables auth. llm_defaults fills any LLM settings
    field the user has not saved via PUT /api/settings (api_key included);
    saved values always win."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    avatars_dir = data_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir / "hub.db")
    if not store.list_personas():  # first start on an empty data dir: seed Riko
        store.add_persona(pid=SEED_PERSONA["id"], **{k: v for k, v in SEED_PERSONA.items() if k != "id"})
        store.set_active(SEED_PERSONA["id"])
        store.set_version(1)

    brain = _brain.Brain()
    llm_defaults = dict(llm_defaults or {})
    app = FastAPI(title="riko-hub", version=HUB_VERSION, docs_url=None, redoc_url=None)
    chat_lock = threading.Lock()  # one chat at a time; concurrent requests queue

    # ── helpers ─────────────────────────────────────────────────────────
    def llm_settings() -> dict:
        merged = dict(_brain.LLM_DEFAULTS)
        merged.update(llm_defaults)
        merged.update(store.get_json("llm", {}))
        return merged

    def switch_phrases() -> list[str]:
        return store.get_json("switch_phrases", list(_brain.DEFAULT_SWITCH_PHRASES))

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
            "active": active,
            "personas": [{"id": p["id"], "name": p["name"]} for p in store.list_personas()],
            "last_message_id": store.last_message_id(),
            "llm_model": brain.cached_model(settings),
        }

    def public_llm(settings: dict) -> dict:
        out = {k: v for k, v in settings.items() if k != "api_key"}
        out["api_key_set"] = bool(settings.get("api_key"))
        return out

    def err(status: int, detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status)

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
        return store.list_personas()

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
        persona = store.add_persona(
            pid, name, keywords,
            str(body.get("system_prompt") or ""),
            str(body.get("greeting") or ""),
            str(body.get("tts_voice") or ""),
        )
        store.bump_version()
        return JSONResponse(persona, status_code=201)

    def persona_or_404(persona_id: str):
        return store.get_persona(persona_id)

    @app.get("/api/personas/{persona_id}")
    def get_persona(persona_id: str):
        persona = persona_or_404(persona_id)
        return persona or err(404, f"no persona '{persona_id}'")

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
        if "keywords" in body:
            if not isinstance(body["keywords"], list):
                return err(422, "keywords must be a list")
            fields["keywords"] = [str(k).strip().lower() for k in body["keywords"] if str(k).strip()]
        persona = store.update_persona(persona_id, fields) if fields else store.get_persona(persona_id)
        store.bump_version()
        return persona

    @app.delete("/api/personas/{persona_id}", status_code=204)
    def delete_persona(persona_id: str):
        if not store.persona_exists(persona_id):
            return err(404, f"no persona '{persona_id}'")
        remaining = [p["id"] for p in store.list_personas() if p["id"] != persona_id]
        if not remaining:
            return err(409, "cannot delete the last persona")
        was_active = store.active() == persona_id
        store.delete_persona(persona_id)
        (avatars_dir / f"{persona_id}.vrm").unlink(missing_ok=True)
        if was_active:
            store.set_active(remaining[0])
        store.bump_version()
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
        store.bump_version()
        return persona

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
            store.bump_version()
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
        store.clear_messages(pid)
        store.bump_version()
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
            store.bump_version()
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
        norm = normalize_switch_text(text)

        def turn():  # blocking section: store + LLM, one chat at a time
            with chat_lock:
                target = match_switch(norm)
                if target:  # switch: activate, greet, store nothing
                    if store.set_active(target):
                        store.bump_version()
                    persona = store.get_persona(target) or {}
                    reply = persona.get("greeting") or f"{persona.get('name', target)} here."
                    return ("ok", {"reply": reply, "persona": target, "switched": True,
                                   "message_ids": [], "state_version": store.version()})

                pid = override or store.active()
                persona = store.get_persona(pid)
                if not persona:
                    return ("err", 404, f"no persona '{pid}'")
                settings = llm_settings()
                if not settings.get("base_url"):
                    return ("err", 503, "LLM not configured")

                max_history = int(settings.get("max_history") or 40)
                history = store.recent_messages(pid, max_history)
                messages = ([{"role": "system", "content": persona["system_prompt"]}]
                            + [{"role": m["role"], "content": m["content"]} for m in history]
                            + [{"role": "user", "content": text}])
                try:
                    answer = brain.chat(settings, messages)
                except Exception as e:
                    return ("err", 502, f"{type(e).__name__}: {e}")

                user_id = store.add_message(pid, "user", text, source)
                assistant_id = store.add_message(pid, "assistant", answer, source)
                store.bump_version()
                return ("ok", {"reply": answer, "persona": pid, "switched": False,
                               "message_ids": [user_id, assistant_id],
                               "state_version": store.version()})

        kind, *rest = await run_in_threadpool(turn)
        return rest[0] if kind == "ok" else err(rest[0], rest[1])

    @app.get("/api/settings")
    def get_settings():
        return {"llm": public_llm(llm_settings()), "switch_phrases": switch_phrases()}

    @app.put("/api/settings")
    async def put_settings(request: Request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return err(400, "body must be a JSON object")
        saved_llm = dict(store.get_json("llm", {}))
        llm = body.get("llm")
        if llm is not None:
            if not isinstance(llm, dict):
                return err(422, "llm must be an object")
            for key in ("base_url", "model", "reasoning_effort"):
                if key in llm:
                    saved_llm[key] = str(llm[key] or "").strip()
            if "api_key" in llm:  # write-only; "" clears
                saved_llm["api_key"] = str(llm["api_key"] or "")
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
            store.set_json("switch_phrases", [p.strip().lower() for p in phrases if p.strip()])
        store.bump_version()
        return {"llm": public_llm(llm_settings()), "switch_phrases": switch_phrases()}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


    # ── standalone ──────────────────────────────────────────────────────
    app.state.store = store
    app.state.brain = brain
    return app


def main():
    import uvicorn

    data_dir = os.getenv("RIKO_HUB_DATA") or str(Path.cwd() / "data")
    llm_defaults = {
        "base_url": os.getenv("RIKO_LLM_BASE_URL", ""),
        "api_key": os.getenv("RIKO_LLM_API_KEY", ""),
        "model": os.getenv("RIKO_LLM_MODEL", "auto"),
    }
    app = create_app(data_dir, api_key=os.getenv("RIKO_HUB_KEY") or None,
                     llm_defaults=llm_defaults)
    host = os.getenv("RIKO_HUB_HOST", "0.0.0.0")
    port = int(os.getenv("RIKO_HUB_PORT", "8002"))
    print(f"[hub] data={data_dir} llm={llm_defaults['base_url'] or '(not configured)'}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
