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

# deps first, so code edits don't bust the install layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && rm -rf /root/.cache/uv

COPY __init__.py server.py store.py brain.py ./
COPY static/ ./static/
COPY assets/ ./assets/

# non-root, but /data is world-writable so `user:` overrides in compose still work
RUN useradd -m -u 1000 hub \
    && mkdir -p /data/avatars \
    && chown -R hub:hub /app \
    && chmod -R a+w /data
USER hub

EXPOSE 8002
# `python server.py` with cwd=/app: sibling imports work via the script dir
CMD ["/app/.venv/bin/python", "server.py"]
