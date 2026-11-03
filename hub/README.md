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
RIKO_HUB_KEY=*** uv run server.py
```

Data goes to `./data` (override with `RIKO_HUB_DATA`), the port is `8002`
(`RIKO_HUB_PORT`), and an empty `RIKO_HUB_KEY` disables auth. Then open
<http://127.0.0.1:8002/> for the web config page.

Desktop mode needs no separate process at all: `desktop.py` with
`hub.mode: local` mounts this same app in-process.

## Deploy with Docker (the AI box)

```bash
mkdir -p ~/AI/Docker/riko-hub && cd ~/AI/Docker/riko-hub
# copy this whole hub/ folder here (rsync -a repo/hub/ ./)
cp .env.example .env      # set RIKO_HUB_KEY, RIKO_LLM_BASE_URL, RIKO_LLM_API_KEY
docker compose up -d      # builds the image on first run
```

Open `http://<box>:8002` and paste the hub key into the page. The health
check hits `/api/health` every 30 s; `docker compose ps` shows the status.

## The data folder (`./data`)

```
data/
  hub.db            personas, messages, settings, state version (SQLite, WAL)
  avatars/<id>.vrm  uploaded avatars (the built-in default is inside the image)
```

Back up (or move) the whole folder — that is all she is. `docker compose down`
keeps it; `docker compose down -v` too (no named volumes are used here).

## Security

The hub speaks plain HTTP with one shared bearer key — run it on a **LAN or
VPN only**, never directly on the internet. The key is required on every
`/api/*` route except `GET /api/health`. The LLM API key is stored in the
hub database (write-only via the API) or in `.env`; never in the browser.
