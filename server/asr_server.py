"""Riko's remote ears: a small Faster-Whisper server with an OpenAI-compatible API.

Run this on the machine with the GPU (or just more CPU). Riko records on your
desktop and sends each clip here as a WAV file; this answers with the text.

    cd server
    uv run asr_server.py                         # CPU
    uv run --extra cuda asr_server.py --device cuda   # NVIDIA GPU (pulls cuBLAS + cuDNN wheels)

Options (or the env vars in brackets):
    --host 0.0.0.0  --port 8001                  [ASR_HOST, ASR_PORT]
    --model large-v3-turbo                       [ASR_MODEL]  any Faster-Whisper model name or path
    --device auto|cuda|cpu  --compute-type auto  [ASR_DEVICE, ASR_COMPUTE_TYPE]
    ASR_API_KEY=secret  -> clients must send "Authorization: Bearer secret"

Endpoints:
    GET  /health
    GET  /v1/models
    POST /v1/audio/transcriptions   (multipart: file, [model], [language], [prompt], [response_format], [temperature])
"""
import argparse
import ctypes
import glob
import io
import os
import site
import threading
import time

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse


def _preload_cuda_libs():
    """Make pip-installed cuBLAS / cuDNN (the `cuda` extra) visible to CTranslate2 without LD_LIBRARY_PATH."""
    roots = [*site.getsitepackages(), site.getusersitepackages()]
    for pattern in ("nvidia/cublas/lib/libcublasLt.so.*", "nvidia/cublas/lib/libcublas.so.*",
                    "nvidia/cudnn/lib/libcudnn*.so.*"):
        for root in roots:
            for lib in sorted(glob.glob(os.path.join(root, pattern))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def parse_args():
    env = os.environ.get
    p = argparse.ArgumentParser(description="Faster-Whisper server with an OpenAI-compatible API.")
    p.add_argument("--host", default=env("ASR_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(env("ASR_PORT", "8001")))
    p.add_argument("--model", default=env("ASR_MODEL", "large-v3-turbo"))
    p.add_argument("--device", default=env("ASR_DEVICE", "auto"), choices=["auto", "cuda", "cpu"])
    p.add_argument("--compute-type", default=env("ASR_COMPUTE_TYPE", "auto"),
                   help="auto = float16 on GPU, int8 on CPU")
    return p.parse_args()


def build_app(model_name, device, compute_type):
    if device in ("auto", "cuda"):
        _preload_cuda_libs()
    from faster_whisper import WhisperModel  # after the preload, so it finds the CUDA libs

    if compute_type == "auto":
        compute_type = "int8" if device == "cpu" else "auto"  # CTranslate2's "auto" = fastest the device supports
    print(f"[asr-server] loading {model_name} (device={device}, compute_type={compute_type}); "
          "the first run downloads it...", flush=True)
    whisper = WhisperModel(model_name, device=device, compute_type=compute_type)
    print(f"[asr-server] ready on {whisper.model.device}", flush=True)
    lock = threading.Lock()  # one clip at a time; keeps GPU memory flat
    api_key = os.environ.get("ASR_API_KEY", "")

    def auth(authorization: str = Header(default="")):
        if api_key and authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="invalid api key")

    app = FastAPI(title="Riko ASR")

    @app.get("/health")
    def health():
        return {"status": "ok", "model": model_name}

    @app.get("/v1/models", dependencies=[Depends(auth)])
    def models():
        return {"object": "list", "data": [{"id": model_name, "object": "model", "owned_by": "faster-whisper"}]}

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(auth)])
    def transcriptions(
        file: UploadFile = File(...),
        model: str = Form(default=""),  # accepted for compatibility; this server has one model
        language: str = Form(default=""),
        prompt: str = Form(default=""),
        response_format: str = Form(default="json"),
        temperature: float = Form(default=0.0),
    ):
        audio = io.BytesIO(file.file.read())
        started = time.monotonic()
        with lock:
            segments, info = whisper.transcribe(
                audio,
                language=language or None,
                initial_prompt=prompt or None,
                temperature=temperature,
                beam_size=5,
                vad_filter=True,  # skips silence, so a long press with little speech stays fast
            )
            text = "".join(s.text for s in segments).strip()
        print(f"[asr-server] {info.duration:.1f}s audio -> {len(text)} chars "
              f"in {time.monotonic() - started:.2f}s ({info.language})", flush=True)
        if response_format == "text":
            return PlainTextResponse(text)
        if response_format == "verbose_json":
            return {"text": text, "language": info.language, "duration": info.duration}
        return {"text": text}

    return app


def main():
    args = parse_args()
    app = build_app(args.model, args.device, args.compute_type)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
