#!/bin/sh
# First start pulls the GGUF weights into /data/models (resume-safe: the
# download goes to a .part file and is renamed only when complete), then
# hands everything to breeze-server.
set -eu

MODEL="${BREEZE_MODEL:-breeze-tts-2-q8_0.gguf}"
MODEL_DIR=/data/models
VOICES_DIR=/data/voices
BASE_URL="https://huggingface.co/HoppouAI/Breeze-TTS-2.cpp/resolve/main"

mkdir -p "$MODEL_DIR" "$VOICES_DIR"

if [ ! -f "$MODEL_DIR/$MODEL" ]; then
    echo "[breeze] downloading $MODEL into $MODEL_DIR (one-time, may take a while)..."
    # -C - resumes a interrupted .part; the rename is atomic so a half file
    # is never mistaken for a finished model.
    curl -fL -C - --progress-bar -o "$MODEL_DIR/$MODEL.part" "$BASE_URL/$MODEL"
    mv "$MODEL_DIR/$MODEL.part" "$MODEL_DIR/$MODEL"
    echo "[breeze] model ready"
fi

# BREEZE_ARGS is split on purpose (e.g. "--webui --parallel 2" or "--cpu")
exec /opt/breeze/breeze-server "$MODEL_DIR/$MODEL" \
    --host 0.0.0.0 --port 8137 \
    --voices-dir "$VOICES_DIR" --ws-port -1 \
    ${BREEZE_ARGS:-}
