# Voice round: Breeze-TTS-2 on the hub

Status: contract for the "voice" round. Children follow it literally and report problems
instead of editing this file. Builds on docs/DESIGN.md (hub API v1). Everything here is
additive: existing routes, fields and client TTS modes keep working.

## Goal

Each persona has its own voice. The **hub** makes the audio (so every client — desktop,
web page, a future Android app — hears the same voice without any TTS setup or keys of
its own). The voice engine is **Breeze-TTS-2** through the C++/GGUF server
[HoppouAI/Breeze-TTS-2.cpp](https://github.com/HoppouAI/Breeze-TTS-2.cpp) (`breeze-server`,
Vulkan, works on NVIDIA, AMD and Intel). Breeze speaks **English and Chinese only**, and its
weights/outputs are **non-commercial** (personal use is fine) — say so in the docs.

```
desktop / android / web ──► hub :8002 ──(docker network "riko")──► breeze-tts :8137 (no auth, not published)
                             │  POST /api/chat  -> reply + audio_url
                             └─ GET  audio_url  -> WAV (made by breeze, cached by the hub)
```

## Facts about breeze-server (v0.1.6, verified by the reviewer)

- Release asset: `https://github.com/HoppouAI/Breeze-TTS-2.cpp/releases/download/v0.1.6/breeze-tts-2-v0.1.6-linux-x64-vulkan.tar.gz`
  sha256 `2b5ca9bd3513d46b66aade07922a6c8127d05588d186febb85d0505c1eb28cf8` (72 MB).
  On Linux it needs the Vulkan loader (`libvulkan1`).
- GGUF weights: Hugging Face repo `HoppouAI/Breeze-TTS-2.cpp`, file `breeze-tts-2-q8_0.gguf`
  (3.3 GB, ~4 GB VRAM, recommended). Others: `q4_k` (2.4 GB), `q6_k`, `f16`.
  URL form `https://huggingface.co/HoppouAI/Breeze-TTS-2.cpp/resolve/main/<file>`.
- `breeze-server <model.gguf> --host 0.0.0.0 --port 8137 --voices-dir <dir> [--webui] [--parallel N] [--ws-port -1]`.
  Warm-up at start (a few seconds). No authentication at all.
- `GET /health` -> liveness + sample rate.
- `POST /v1/audio/speech` with `Content-Type: application/json` (OpenAI style):
  `{"input": text, "voice": <saved voice id, optional>, "instructions": <voice description when
  designing (no voice) / delivery direction when cloning>, "response_format": "wav",
  "seed": int, "cfg_scale": float}` -> streamed 24 kHz mono 16-bit WAV whose header has
  max length fields (read to end of stream; re-save through soundfile before giving it to
  browsers, like riko/tts.py already does for Fish). `404` unknown voice, `409` all slots busy
  (retry), `400` bad request. Unknown `voice` is a 404, never a silent fallback.
- `POST /v1/voices` multipart: `ref_audio=@clip.wav`, `ref_text=<exact transcript>`,
  `name=<[A-Za-z0-9_-]{1,64}>` -> saves `<name>.breeze` in the voices dir (persists across
  restarts). `GET /v1/voices` lists them; `DELETE /v1/voices/<id>` drops from memory only.
- Without a `voice`, every request designs a voice from `instructions`; a fixed `seed` keeps
  it close to stable between requests.

## Persona voice (hub)

Persona gains two fields (old `tts_voice` stays, for clients doing their own TTS):

```json
"voice": {
  "mode": "design",            // "design" | "clone" | "off"
  "description": "A proud, sharp teenage girl. Fast, confident, a little impatient.",
  "style": "",                 // delivery direction, used with clone (and appended for design)
  "ref_text": "",              // exact transcript of the reference clip (clone)
  "has_reference": false,      // read-only: a reference clip is stored
  "seed": 1234,                // random int on persona creation; fixed so the voice stays stable
  "cfg_scale": null            // null = breeze default; 1-5 typical, higher follows instructions more
},
"voice_version": 1             // read-only; +1 on any voice change (caches key on it)
```

Seed persona `riko` gets `mode: design`, a description matching her character (snarky,
playful young woman, quick and teasing), a random seed. Existing personas in an old DB get
`mode: design` with a generic description derived from nothing (e.g. "A young woman with a
clear, friendly voice.") and a random seed — via the store's user_version migration.

Breeze voice id for a clone persona: `riko-<persona_id>-v<voice_version>` (hub registers it
when the reference is uploaded; on a 404 from breeze it re-registers from the stored clip
and retries once — breeze may have been reset).

## Hub settings

`GET/PUT /api/settings` gains a `tts` object (same "blank = server default" semantics as `llm`):
`{"enabled": true, "base_url": "http://breeze-tts:8137", "timeout": 120, "parallel_note": ""}`
plus `defaults.tts` and `llm_source`-style `tts_source`. Env defaults: `RIKO_TTS_BASE_URL`
(empty = TTS off), `RIKO_TTS_ENABLED` (default `1` when a base URL is set).
`POST /api/settings/test` also reports `{"tts": {"ok": bool, "error"?: str}}` (GET /health).

## New / changed hub routes (all need the hub key, like the rest of /api)

| route | body | response |
|---|---|---|
| `POST /api/chat` (changed) | as before, plus optional `"speak": true` (default true) | ChatResult gains `"audio_url": "/api/messages/<assistant id>/audio"` for a normal reply, `"/api/personas/<id>/greeting/audio?v=<voice_version>"` for a switch, or `null` when TTS is off / `speak:false` / persona voice `off`. The chat itself never waits for audio. |
| `GET /api/messages/{id}/audio` | – | `audio/wav` of that assistant message in its persona's current voice. Made on first request, cached on disk as `data/audio/<id>-v<voice_version>.wav`. `404` unknown / not an assistant message, `409` persona voice off, `503 {"detail": ...}` TTS disabled or breeze unreachable, `502` breeze error. |
| `GET /api/personas/{id}/greeting/audio` | – | same, for the persona's greeting (cache `data/audio/greeting-<id>-v<voice_version>.wav`). |
| `POST /api/tts` | `{"text", "persona"?: id (default active)}` | `audio/wav`, not cached (for previews and clients' own lines, max 1000 chars) |
| `PUT /api/personas/{id}/voice` | partial `{mode, description, style, ref_text, seed, cfg_scale}` | Persona (voice_version +1). `mode: clone` without a stored reference -> `422`. |
| `PUT /api/personas/{id}/voice/reference` | multipart `file` (wav/flac/ogg/mp3, ≤ 20 MB, 3–30 s after decoding) + form `ref_text` (required, non-empty) | Persona; stores `data/voices/<id>.wav` (re-encoded mono WAV), registers it on breeze, sets `mode: clone`, voice_version +1. `400` bad audio/length, `502/503` breeze problems (then nothing is changed). |
| `GET /api/personas/{id}/voice/reference` | – | the stored clip (`audio/wav`) or `404` |
| `DELETE /api/personas/{id}/voice/reference` | – | Persona; deletes clip, mode back to `design`, voice_version +1 |
| `POST /api/personas/{id}/voice/preview` | `{"text"?: str, "voice"?: {unsaved voice fields}}` (default: the greeting or a stock line; overrides are used once, nothing stored) | `audio/wav` (not cached) — the web page's "Preview" |
| `POST /api/personas/{id}/voice/lock` | `{"text"?: str}` | Persona. Design → clone: generate the text in the current design voice (with its seed), store it as the reference with that text as `ref_text`, register it, `mode: clone`. Freezes a designed voice you like. |
| `GET /api/tts/status` | – | `{"enabled", "reachable", "base_url", "voices": n}` |

Audio cache: total size cap 500 MB (oldest files removed first); deleting a persona or
clearing its history deletes its cached audio. Only one breeze request per persona at a
time is not required — breeze queues (409 → retry up to 3× with a short backoff).

## Web config page

Persona editor gets a **Voice** panel: mode selector; description (design) and style
textareas; seed with a 🎲 re-roll; cfg_scale; reference upload with transcript field and an
audio player for the stored clip; **Preview** (plays `/voice/preview`), **Lock this voice**
(design → clone), **Remove reference**. Chat test box plays the reply's `audio_url`.
Settings gets a TTS section (enabled, base URL, test). A note: English and Chinese only.
Audio needs the Bearer key, so fetch it with `fetch()` + `URL.createObjectURL`, not a bare
`<audio src>`.

## Desktop client

New TTS mode `tts: hub` (the new default in character_config.yaml): after a chat (or a
persona switch greeting), GET `audio_url` from the hub with the key, save it to the usual
`client/audio/riko.wav` (re-saved through soundfile) and play it exactly like today (page or
speakers). `audio_url` null → bubble only. Hub TTS error → print once, bubble only, keep
going. Other modes (`none`, `openai`, `sovits`, `fish`) are unchanged and still do their own
TTS on the PC. `riko-ctl` / tray unchanged.

## Deployment (`tts/` directory, new)

`tts/Dockerfile`, `tts/compose.yaml`, `tts/.env.example`, `tts/entrypoint.sh`,
`tts/.dockerignore`, `tts/data/.gitkeep`, `tts/README.md`. House style as `server/` and `hub/`
(no BuildKit features, non-root, `user: "${UID:-1000}:${GID:-1000}"`, `./data:/data`,
restart unless-stopped, healthcheck on `/health`). Image: `debian:bookworm-slim` (or
ubuntu:24.04) + `libvulkan1` + `mesa-vulkan-drivers` (AMD/Intel) + curl + ca-certificates;
download the pinned release tarball **at build time** and verify the sha256. The entrypoint
downloads `${BREEZE_MODEL:-breeze-tts-2-q8_0.gguf}` into `/data/models/` on first start
(to a `.part` file, then rename; resume-safe; print progress) and then execs breeze-server
with `--voices-dir /data/voices --host 0.0.0.0 --port 8137 --ws-port -1` plus
`${BREEZE_ARGS}` (e.g. `--webui --parallel 2`). Healthcheck `start_period` long (first
download).

GPU: compose for NVIDIA reserves the GPU (`driver: nvidia, capabilities: [gpu]`) and sets
`NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics` (Vulkan needs `graphics`; the
container toolkit then injects the NVIDIA Vulkan ICD). `tts/compose.amd.yaml` override:
no NVIDIA reservation, `devices: [/dev/dri]`, `group_add` video/render (numeric GIDs via env,
documented). `tts/compose.cpu.yaml` override adds `--cpu`.

Networking: the compose files of `tts/` and `hub/` both attach to an **external** docker
network named `riko` (one-time `docker network create riko`, documented in both READMEs and
the main README); breeze's service is reachable as `breeze-tts:8137` on it (set
`container_name`/alias `breeze-tts`). Breeze's port is NOT published by default; an optional
commented `ports: ["127.0.0.1:8137:8137"]` line for using its own web UI through an SSH
tunnel. The hub keeps publishing 8002 as before. Local mode on one PC: run the release
binary directly (`breeze-server model.gguf --port 8137`) and set the hub's TTS base URL to
`http://127.0.0.1:8137`; document it (works on AMD via Vulkan).

## Round "voice" file ownership

| path | owner |
|---|---|
| `hub/**` (incl. hub/compose.yaml network change, hub/README.md, static page), `tests/test_hub_*.py`, `docs/API.md` | **H** (hub task) |
| `tts/**` (new), `riko/**`, `main.py`, `desktop.py`, `bin/**`, `client/**`, `character_config.yaml`, `.env.example`, root `pyproject.toml`/`uv.lock`, `tests/` except `test_hub_*.py`, `README.md` | **C** (deploy + client task) |
| `docs/DESIGN.md`, `docs/VOICE.md`, `.gitignore`, `server/**` | reviewer only |
