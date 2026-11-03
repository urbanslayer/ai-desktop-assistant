# riko-breeze-tts — Riko's voice box

[Breeze-TTS-2.cpp](https://github.com/HoppouAI/Breeze-TTS-2.cpp)'s `breeze-server`
in a container. The hub (`../hub/`) calls it over the shared `riko` docker
network and hands the WAV to every client — desktop, web page, a future phone
app — so each persona has one voice everywhere and no client needs any TTS
setup or keys of its own.

> **Two things to know about Breeze:**
> - Its **weights and the audio it produces are non-commercial** (MIT-like code,
>   but the model licence says personal use only). Fine for your own Riko.
> - It speaks **English and Chinese only**.

```
riko-hub :8002 ──(docker network "riko")──► breeze-tts :8137   (no auth, not published)
```

## One-time setup (once per machine, shared with the hub)

```sh
docker network create riko
```

Both this folder and `hub/` attach to that external network; the hub reaches
breeze as `http://breeze-tts:8137`. Breeze's port is **not published** by
default — only the hub talks to it, and it has no authentication.

## Deploy

There are **two images** for two GPU vendors:

| image | built from | for | why |
|---|---|---|---|
| `riko-breeze-tts-cuda` (default) | `Dockerfile.cuda` — compiles Breeze-TTS-2.cpp from source with the CUDA backend | NVIDIA | the prebuilt Vulkan binary's NVIDIA driver ICD fails to load inside containers (`Could not get 'vkCreateInstance' via 'vk_icdGetInstanceProcAddr' for ICD /usr/lib64/libGLX_nvidia.so.0`), even with `NVIDIA_DRIVER_CAPABILITIES=all`; CUDA works in containers fine |
| `riko-breeze-tts` | `Dockerfile` — downloads the pinned prebuilt Vulkan tarball | AMD/Intel GPU, or CPU-only | prebuilt, quick build; Mesa's Vulkan works in containers |

Copy this folder to the AI box, e.g. `~/AI/Docker/breeze-tts`, then:

```sh
cp .env.example .env       # optional; everything has defaults
docker compose up -d       # NVIDIA box (CUDA image, built from source)
# AMD/Intel GPU (Vulkan through Mesa):
#   set VIDEO_GID/RENDER_GID in .env first — numeric gids from:  getent group video render
docker compose -f compose.yaml -f compose.amd.yaml up -d
# No GPU at all:
docker compose -f compose.yaml -f compose.cpu.yaml up -d
```

The CUDA build needs no GPU on the build machine (it also builds on AMD boxes),
but the **first build takes a while** — it pulls several GB of CUDA devel
images and compiles the ggml CUDA kernels. Afterwards it's cached. By default
it bakes in sm 86 + 89 + 120 (Ampere/Ada/Blackwell); set `CUDA_ARCHS` in
`.env` to just your card's arch for a much faster build, e.g. `CUDA_ARCHS=120`
(RTX 50xx), `89` (RTX 40xx), `86` (RTX 30xx), `75` (Turing).

First start downloads the model into `./data/models/` (resumable — the
healthcheck's long `start_period` covers it), then warms the GPU and answers
`GET /health`. `./data/` holds models and cloned voices and survives rebuilds;
it stays owned by your host user (`UID`/`GID` in `.env`).

On the hub's web page (Settings) point the TTS base URL at
`http://breeze-tts:8137` — that's the default the hub ships with.

## Which model?

`BREEZE_MODEL` in `.env` (files from
[Hugging Face](https://huggingface.co/HoppouAI/Breeze-TTS-2.cpp), downloaded
once into `./data/models/`):

| file | size | VRAM (approx) | notes |
|---|---|---|---|
| `breeze-tts-2-q8_0.gguf` | 3.3 GB | ~4 GB | **recommended**, the default |
| `breeze-tts-2-q4_k.gguf` | 2.4 GB | ~3 GB | fits smaller cards; shares room with ASR (~1.1 GB) |
| `breeze-tts-2-q6_k.gguf` | 2.9 GB | ~3.5 GB | a bit better than q8_0 |
| `breeze-tts-2-f16.gguf` | 5.9 GB | ~6.5 GB | best quality |

Other flags go in `BREEZE_ARGS`, e.g. `BREEZE_ARGS=--webui --parallel 2`.

## Breeze's own web page (optional)

Uncomment the `ports:` line in `compose.yaml` (it binds `127.0.0.1` only) and
add `--webui` to `BREEZE_ARGS`, then tunnel from your PC:

```sh
ssh -L 8137:127.0.0.1:8137 <ai-box>   # open http://127.0.0.1:8137 locally
```

## No docker? Local mode on one PC

Run the release binary straight from the [releases
page](https://github.com/HoppouAI/Breeze-TTS-2.cpp/releases) (linux-x64-vulkan
tarball; needs `libvulkan1`, and `mesa-vulkan-drivers` on AMD/Intel):

```sh
./breeze-server breeze-tts-2-q8_0.gguf --port 8137 --voices-dir voices
```

and set the hub's TTS base URL to `http://127.0.0.1:8137`. Vulkan means this
works the same on NVIDIA, AMD and Intel GPUs.

## The API breeze serves (the hub uses it; docs/VOICE.md has the details)

- `GET /health` — liveness + sample rate (this container's healthcheck)
- `POST /v1/audio/speech` — OpenAI-style JSON, streams a 24 kHz WAV;
  without a `voice` it *designs* one from `instructions` (fixed `seed` keeps
  it stable), with a `voice` it clones it
- `POST /v1/voices` — multipart `ref_audio` + `ref_text` + `name` → saves a
  `<name>.breeze` voice into `./data/voices/` (persists across restarts)

## Notes

- GPU memory on the 16 GB AI box: ASR ~1.1 GB + Breeze q8_0 ~4 GB — plenty of room.
- The Vulkan image is `debian:trixie-slim` on purpose: its Mesa is new enough
  for RDNA4 cards (RX 90xx); bookworm's is not. The CUDA image is
  `nvidia/cuda:12.9.1-runtime-ubuntu24.04` (built with the 12.9 devel image —
  Blackwell needs CUDA >= 12.8).
- `docker compose logs -f breeze-tts` shows the device it picked at startup
  (`backend: Vulkan0`/`CUDA0`/`CUDA1, sample rate: 24000` = the GPU;
  `backend: CPU` = the GPU isn't reaching the container).
- The CUDA binary is pinned to the same source as the release: tag `v0.1.6`,
  commit `87b43667ca5597f826441991e416a279ed7373b9` (the build fails if the
  tag ever moves). Both images speak the identical HTTP API docs/VOICE.md
  describes — the hub doesn't care which one runs.
