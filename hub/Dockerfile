# Riko's shared brain: the hub server (personas, memory, LLM settings).
# House style matches server/ (the ASR image): plain python-slim base, uv used
# only at build time (plain RUN — the docker hosts here have no BuildKit, so
# no `RUN --mount`), non-root user, /data world-writable for `user:` overrides.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    RIKO_HUB_DATA=/data \
    RIKO_HUB_HOST=0.0.0.0 \
    RIKO_HUB_PORT=8002

WORKDIR /app

# uv, used only at build time — the image runs the venv python directly
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# MCP servers launched by the hub run INSIDE this container (R2-M): node+npm
# give `npx @modelcontextprotocol/server-*`, /bin/uvx above gives the
# `uvx mcp-server-*` family. Tool caches live under /data so the uvx/npm
# downloads survive restarts and work for the non-root hub user.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# deps first, so code edits don't bust the install layer (root's uv cache is
# dropped in the same layer — the runtime UV_CACHE_DIR below lives under /data)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && rm -rf /root/.cache/uv

# Runtime caches for MCP servers launched via uvx/npx: /data is a volume and
# world-writable, so the non-root hub user can (re)download tool packages.
ENV UV_CACHE_DIR=/data/cache/uv \
    npm_config_cache=/data/cache/npm \
    XDG_CACHE_HOME=/data/cache/xdg

# Whole source tree in one layer — .dockerignore keeps .venv/, __pycache__/,
# data/ and .env out. A hand-written file list here crash-looped the deployed
# hub when clients.py/emotion.py/events.py/toolloop.py/tools/ were added
# (ModuleNotFoundError); tests/test_hub_packaging.py keeps that from recurring.
COPY . ./

# non-root, but /data is world-writable so `user:` overrides in compose still work
RUN useradd -m -u 1000 hub \
    && mkdir -p /data/avatars /data/cache/uv /data/cache/npm /data/cache/xdg \
    && chown -R hub:hub /app /home/hub \
    && chown -R hub:hub /data/cache \
    && chmod -R a+w /data
USER hub

EXPOSE 8002
# `python server.py` with cwd=/app: sibling imports work via the script dir
CMD ["/app/.venv/bin/python", "server.py"]
