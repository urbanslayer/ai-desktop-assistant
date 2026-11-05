"""Riko's remote ears: a small Faster-Whisper server with an OpenAI-compatible API.

Run this on the machine with the GPU (or just more CPU). Riko records on your
desktop and sends each clip here as a WAV file; this answers with the text.

    cd server
    uv run asr_server.py                         # CPU
    uv run --extra cuda asr_server.py --device cuda   # NVIDIA GPU (pulls cuBLAS + cuDNN wheels)

Options (or the env vars in brackets):
    --host 0.0.0.0  --port 8001                  [ASR_HOST, ASR_PORT]
    --model large-v3-turbo                       [ASR_MODEL]  any Faster-Whisper model name or path
    --model-dir /data/models                     [ASR_MODEL_DIR]  where to download models (default: Faster-Whisper's usual HF cache)
    --device auto|cuda|cpu  --compute-type auto  [ASR_DEVICE, ASR_COMPUTE_TYPE]
    ASR_API_KEY=secret  -> clients must send "Authorization: Bearer secret"

Endpoints:
    GET  /health
    GET  /v1/models
    POST /v1/audio/transcriptions   (multipart: file, [model], [language], [prompt], [hotwords], [response_format], [temperature])

`prompt` maps to Faster-Whisper's initial_prompt (its decoder context:
spellings and style it should prefer, e.g. "Riko, Furina, senpai."), and
`hotwords` to Faster-Whisper's hotwords parameter. In faster-whisper >= 1.1
the two COMPOSE rather than compete: the prompt tokens go in first and the
hotword tokens are appended after them, so sending both is fine and this
server forwards both. (Servers on an older faster-whisper without a
hotwords parameter just ignore the field, see build_app.)
"""
import argparse
import ctypes
import glob
import hmac
import inspect
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
    p.add_argument("--model-dir", default=env("ASR_MODEL_DIR") or None,
                   help="directory to download models into (default: Faster-Whisper's usual HF cache)")
    return p.parse_args()


def build_app(model_name, device, compute_type, model_dir=None):
    if device in ("auto", "cuda"):
        _preload_cuda_libs()
    from faster_whisper import WhisperModel  # after the preload, so it finds the CUDA libs

    if compute_type == "auto":
        compute_type = "int8" if device == "cpu" else "auto"  # CTranslate2's "auto" = fastest the device supports
    print(f"[asr-server] loading {model_name} (device={device}, compute_type={compute_type}); "
          "the first run downloads it...", flush=True)
    print(f"[asr-server] model files: "
          + (f"{os.path.abspath(model_dir)} (--model-dir)" if model_dir
             else os.environ.get("HF_HOME") or "the default Hugging Face cache (~/.cache/huggingface)"), flush=True)
    whisper = WhisperModel(model_name, device=device, compute_type=compute_type,
                           download_root=model_dir)
    print(f"[asr-server] ready on {whisper.model.device}", flush=True)
    # faster-whisper >= 1.1 has a `hotwords` parameter (its tokens are appended
    # after the initial_prompt ones — the two compose, they are not exclusive).
    # An older install gets the field accepted but ignored, per the docstring.
    supports_hotwords = "hotwords" in inspect.signature(whisper.transcribe).parameters
    if not supports_hotwords:
        print("[asr-server] this faster-whisper has no 'hotwords' parameter; "
              "the hotwords form field will be ignored", flush=True)
    lock = threading.Lock()  # one clip at a time; keeps GPU memory flat
    api_key = os.environ.get("ASR_API_KEY", "")

    def auth(authorization: str = Header(default="")):
        if api_key and not hmac.compare_digest(authorization, f"Bearer {api_key}"):
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
        prompt: str = Form(default=""),   # -> initial_prompt (vocabulary spelling bias)
        hotwords: str = Form(default=""),  # -> faster-whisper hotwords (>= 1.1 only)
        response_format: str = Form(default="json"),
        temperature: str = Form(default=""),  # str so a client sending "" does not 422
    ):
        try:
            temp = float(temperature) if temperature else 0.0
        except ValueError:
            temp = 0.0
        audio = io.BytesIO(file.file.read())
        started = time.monotonic()
        kwargs = dict(language=language or None,
                      initial_prompt=prompt or None,
                      temperature=temp,
                      beam_size=5,
                      vad_filter=True)  # skips silence, so a long press with little speech stays fast
        if hotwords and supports_hotwords:
            kwargs["hotwords"] = hotwords
        with lock:
            segments, info = whisper.transcribe(audio, **kwargs)
            # the generator materialises lazily: drain it inside the lock
            seg_list = list(segments)
        text = "".join(s.text for s in seg_list).strip()
        print(f"[asr-server] {info.duration:.1f}s audio -> {len(text)} chars "
              f"in {time.monotonic() - started:.2f}s ({info.language})", flush=True)
        if response_format == "text":
            return PlainTextResponse(text)
        if response_format == "verbose_json":
            # OpenAI's verbose shape. The per-segment confidence fields are
            # what Riko's client uses to tell a real "Thank you." from
            # Whisper inventing words on a noise clip — keep them.
            return {"text": text, "language": info.language, "duration": info.duration,
                    "segments": [{"id": i, "start": s.start, "end": s.end, "text": s.text,
                                  "temperature": float(s.temperature),
                                  "avg_logprob": float(s.avg_logprob),
                                  "compression_ratio": float(s.compression_ratio),
                                  "no_speech_prob": float(s.no_speech_prob)}
                                 for i, s in enumerate(seg_list)]}
        return {"text": text}

    return app


def main():
    args = parse_args()
    app = build_app(args.model, args.device, args.compute_type, model_dir=args.model_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
