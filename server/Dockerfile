# Riko's remote ears: Faster-Whisper server, CUDA build.
# The base image is plain Python — the CUDA libraries (cuBLAS/cuDNN) come from
# the `cuda` extra's pip wheels; the NVIDIA container toolkit injects only the
# driver at runtime.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    ASR_MODEL_DIR=/data/models \
    HF_HOME=/data/hf \
    ASR_DEVICE=cuda \
    ASR_PORT=8001

WORKDIR /app

# uv, used only at build time — the image runs the venv python directly
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# deps first, so code edits don't bust the install layer
# (plain RUN, no --mount: must also build with the legacy builder, no BuildKit)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra cuda --no-dev && rm -rf /root/.cache/uv

COPY asr_server.py ./

# non-root, but /data is world-writable so `user:` overrides in compose still work
RUN useradd -m -u 1000 asr \
    && mkdir -p /data/models /data/hf \
    && chown -R asr:asr /app \
    && chmod -R a+w /data
USER asr

EXPOSE 8001
CMD ["/app/.venv/bin/python", "asr_server.py"]
