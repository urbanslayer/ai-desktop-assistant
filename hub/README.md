# riko-hub

One small server that holds Riko's **brain**: personas (prompts, greetings,
VRM avatars), the shared chat history, and the LLM settings. Every client —
the desktop app, a terminal, the Android app, this page — talks the same HTTP
API, so a conversation continues where the last device stopped and personas
can be switched mid-chat by keyword.

Full API reference for client authors: [`docs/API.md`](../docs/API.md).
The design contract: [`docs/DESIGN.md`](../docs/DESIGN.md).

## Run it locally (from this folder)

```bash
cd hub
RIKO_LLM_BASE_URL=http://192.0.2.51:8000/v1 RIKO_LLM_API_KEY=*** \
RIKO_HUB_KEY=*** \
RIKO_TTS_BASE_URL=http://127.0.0.1:8137 uv run server.py
```

Data goes to `./data` (override with `RIKO_HUB_DATA`), the port is `8002`
(`RIKO_HUB_PORT`), and an empty `RIKO_HUB_KEY` disables auth. An empty
`RIKO_TTS_BASE_URL` (or `RIKO_TTS_ENABLED=0`) turns voice off everywhere —
every `audio_url` stays `null` and the audio routes answer 503. Then open
<http://127.0.0.1:8002/> for the web config page.

Desktop mode needs no separate process at all: `desktop.py` with
`hub.mode: local` mounts this same app in-process.

## Deploy with Docker (the AI box)

```bash
mkdir -p ~/AI/Docker/riko-hub && cd ~/AI/Docker/riko-hub
# copy this whole hub/ folder here (rsync -a repo/hub/ ./)
cp .env.example .env      # set RIKO_HUB_KEY, RIKO_LLM_BASE_URL, RIKO_LLM_API_KEY
docker network create riko   # once per host: the shared network for hub + breeze
docker compose up -d      # builds the image on first run
```

Open `http://<box>:8002` and paste the hub key into the page. The health
check hits `/api/health` every 30 s; `docker compose ps` shows the status.

## Environment variables (compose / .env)

`compose.yaml` forwards every variable the hub reads (see `hub/server.py`
`main()`; `tests/test_hub_packaging.py` fails if one drifts):

| Var | Default | Meaning |
| --- | --- | --- |
| `RIKO_HUB_KEY` | empty (open LAN) | bearer key required on `/api/*` |
| `RIKO_HUB_PORT` | `8002` | **host** port published to the container's 8002 |
| `RIKO_LLM_BASE_URL` / `RIKO_LLM_API_KEY` / `RIKO_LLM_MODEL` | `""` / `""` / `auto` | OpenAI-compatible LLM defaults |
| `RIKO_TTS_BASE_URL` / `RIKO_TTS_ENABLED` | `http://breeze-tts:8137` / `1` | Breeze-TTS-2; empty URL = voice off |
| `RIKO_SEARXNG_URL` | empty (web_search off) | SearXNG backend for `web_search`, e.g. `http://192.0.2.101:8989` |
| `RIKO_HA_URL` + `RIKO_HA_TOKEN` | empty (off) | built-in `homeassistant` MCP server: HA base URL + long-lived access token; both set enables `…/api/mcp/assist` (README "Home Assistant") |
| `RIKO_ALLOW_PRIVATE_FETCH` | `0` | `1` lets `fetch_page` reach private/loopback IPs (SSRF guard off) |
| `RIKO_HUB_DATA` / `RIKO_HUB_HOST` / `RIKO_HUB_PORT` (in-container) | `/data` / `0.0.0.0` / `8002` | baked into the image; compose pins them, see `.env.example` |

## Voice (Breeze-TTS-2)

The hub is the **only** component that talks to breeze
([`docs/VOICE.md`](../docs/VOICE.md)); clients just fetch the `audio_url`
paths the hub returns and always get properly-headed WAVs. The breeze stack
(`breeze-tts` service, port 8137) lives in its own compose project; both
stacks join one external network, created **once** on the host:

```bash
docker network create riko
```

`.env` then points the hub at it: `RIKO_TTS_BASE_URL=http://breeze-tts:8137`
(from outside docker: `http://<gpu-box>:8137`) and `RIKO_TTS_ENABLED=1`.
Empty base URL = voice off; the switch is also in the web Settings page
(only `base_url`/`parallel_note` blank back to these env defaults). Each
persona's voice (design description / clone reference / off) is edited per
persona; the hub synthesises replies lazily and caches them per
`voice_version`, so persona voices, previews, and locked voices never
re-synthesise on every read. Breeze speaks English and Chinese only, and
its weights are non-commercial.

## MCP servers (the hub as an MCP client)

Settings > **MCP** on the web page (or `PUT /api/settings` with an `mcp`
block) attaches external MCP servers; their tools reach the model as
`mcp_<server>__<tool>`, with the same permissions, tool log and events as the
built-ins — see [`docs/API.md`](../docs/API.md) *MCP servers* for the full
contract, `GET /api/mcp/status` and `POST /api/mcp/{name}/restart` for health
and manual reconnect.

Reference-server examples that work out of the box in the Docker image
(`uvx` and `npx` are installed there; their caches are writable under
`/data/cache/`):

```jsonc
// time — official reference server via uv
{"name": "time", "transport": "stdio", "enabled": true,
 "command": "uvx", "args": ["mcp-server-time"]}

// fetch — official reference server via uv
{"name": "fetch", "transport": "stdio", "enabled": true,
 "command": "uvx", "args": ["mcp-server-fetch"]}

// filesystem — official reference server via npx; the folder must be
// mounted into the hub container (compose: `- /srv/data:/srv/data:ro`)
{"name": "files", "transport": "stdio", "enabled": true,
 "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
                            "/srv/data"]}
```

stdio servers are **children of the hub container** — they see its filesystem
and network, not yours. Anything on your machine needs a bind mount in
`compose.yaml` (volumes), and anything on another box is better served over
`transport: "http"`. First `uvx`/`npx` launch downloads the package (the
status row stays `starting` meanwhile, then goes `ready`).

Local (non-docker) mode: the hub imports the MCP SDK **lazily**, so it still
boots where `mcp` isn't installed — every server just reports
`unavailable: mcp package not installed`. For MCP to actually work from the
root project (`desktop.py` local mode), install it into the same environment
the root `pyproject.toml` resolves (add `mcp` there too; hub/ pins it).

## The data folder (`./data`)

```
data/
  hub.db            personas, messages, settings, voices, state version (SQLite, WAL)
  avatars/<id>.vrm  uploaded avatars (the built-in default is inside the image)
  voices/<id>.wav   per-persona reference clips (the only voice source that persists)
  audio/            cached reply/greeting WAVs (<msg>-v<voicever>.wav, capped at 500 MB)
  cache/            uv/npm/xdg caches for MCP stdio servers (uvx / npx downloads)
```

Back up (or move) the whole folder — that is all she is. `docker compose down`
keeps it; `docker compose down -v` too (no named volumes are used here).

## Settings semantics (the config page can't shoot itself in the foot)

`PUT /api/settings` only changes what you send, and **blank means "unset"**:
a blank `base_url` / `model` / `reasoning_effort` deletes that saved override
so the default applies again (defaults = built-ins overlaid with the
`RIKO_LLM_*` env the server started with; `model` then means `auto`). The LLM
`api_key` only changes when the request carries a non-empty value — an empty
or autofilled-blank field can never wipe or shadow `RIKO_LLM_API_KEY`; send
`"clear_api_key": true` to deliberately drop the saved key and fall back to
the env default. `switch_phrases: []` resets the phrases to the defaults
(keywords and names always switch on their own). `POST /api/settings/reset`
drops saved overrides wholesale, and `POST /api/settings/test` answers
`{ok, model}` by listing `/v1/models` with the merged settings. `GET
`/api/settings` returns the merged values plus `defaults` and a per-field
`llm_source` (`saved` vs `default`) — the web page shows where each value
comes from and keeps Save disabled until its first load succeeded. The `tts`
block follows the same rules: blank `base_url` restores the `RIKO_TTS_BASE_URL`
default, `enabled` is a plain boolean, and `POST /api/settings/reset` accepts
`{"tts": true}` too. The `mcp` block's `servers` list is replaced wholesale;
per-server `env`/`headers` **values** are write-only (GET shows key names and
set-flags only; a blank PUT keeps a stored value), and
`POST /api/settings/reset` accepts `{"mcp": true}`.

## Security

The hub speaks plain HTTP with one shared bearer key — run it on a **LAN or
VPN only**, never directly on the internet. The key is required on every
`/api/*` route except `GET /api/health`. The LLM API key is stored in the
hub database (write-only via the API) or in `.env`; never in the browser.
