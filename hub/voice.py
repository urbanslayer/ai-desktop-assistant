"""Breeze-TTS-2.cpp client for the hub (docs/VOICE.md).

The hub is the only thing that talks to breeze; clients just GET the
audio_url the hub hands them. breeze-server (v0.1.6) speaks an OpenAI-ish
dialect:

  GET  /health                -> liveness + sample rate
  POST /v1/audio/speech       -> JSON body, streamed WAV **with max-length
                                 header fields** — read the whole body, then
                                 re-encode through soundfile before any
                                 browser sees it (same trick riko/tts.py uses
                                 for Fish; we do it here via reencode_wav).
  POST /v1/voices             -> multipart ref_audio/ref_text/name, persists
                                 <name>.breeze in the voices dir
  GET  /v1/voices             -> list saved voices
  DELETE /v1/voices/<id>      -> drop from memory only

Status codes that matter: 404 unknown voice (never a silent fallback),
409 all slots busy (retry), 400 bad request.

Breeze speaks **English and Chinese only** and its weights/outputs are
**non-commercial**. No authentication (LAN/VPN service only).

**Text length: no input cap — do not chunk here.** Per Breeze-TTS-2.cpp
v0.1.6 (`docs/server.md`, tag commit 87b4366): "There is no length limit on
`text`" — the server splits long text **on sentence boundaries** itself
(`split_chars`, server default `--split-chars 600`) and generates the pieces
one at a time, each conditioned on the first so the voice does not change
partway. Client-side chunking would be worse: our pieces would be generated
independently and need silence padding between them. So `speech()` sends the
whole utterance as ONE request; the hub's overall spoken-length cap lives in
`hub/speech.py` (`MAX_SPOKEN_CHARS`), which keeps every request within a
sane size regardless.
"""
import io
import time

import requests
import soundfile as sf

# 409 = every breeze slot busy: retry a few times with a short backoff.
BUSY_RETRIES = 3
BUSY_BACKOFF = 0.4  # seconds, doubled per attempt


class TtsError(Exception):
    """breeze answered with something we cannot use (or a transport failure)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TtsUnreachable(TtsError):
    """Could not talk to breeze at all (down, wrong URL, timeout connect)."""


class TtsNotFound(TtsError):
    """404: the voice id breeze was asked for does not exist (it may have been reset)."""


class TtsBusy(TtsError):
    """409: every generation slot stayed busy through every retry."""


def reencode_wav(raw: bytes) -> bytes:
    """Re-save breeze's WAV through soundfile.

    breeze streams 24 kHz mono 16-bit WAV whose RIFF/data length fields are
    max-length placeholders — browsers believe them and never fire 'ended'.
    Reading the decoded audio and writing a fresh WAV fixes the header."""
    data, sample_rate = sf.read(io.BytesIO(raw))
    if data.ndim > 1:  # downmix to mono (stereo inputs are rare but legal)
        data = data.mean(axis=1)
    out = io.BytesIO()
    sf.write(out, data, sample_rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


def decode_upload(raw: bytes) -> tuple:
    """Decode an uploaded reference clip (wav/flac/ogg/mp3 — whatever this
    libsndfile supports) -> (mono float samples, sample_rate).
    Raises ValueError on anything undecodable."""
    try:
        data, sample_rate = sf.read(io.BytesIO(raw))
    except Exception as e:
        raise ValueError(f"could not decode audio file ({type(e).__name__}: {e}); "
                         "use wav/flac/ogg (mp3 if this build supports it)") from e
    if data.ndim > 1:
        data = data.mean(axis=1)
    if len(data) == 0 or sample_rate <= 0:
        raise ValueError("decoded audio is empty")
    return data, sample_rate


def encode_wav(data, sample_rate: int) -> bytes:
    out = io.BytesIO()
    sf.write(out, data, sample_rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


class Breeze:
    """Thin, stateless-per-call client over the merged `tts` settings object.
    All methods are blocking — call them from a threadpool, not the event loop."""

    def __init__(self, settings: dict):
        self.base_url = str(settings.get("base_url") or "").rstrip("/")
        self.timeout = float(settings.get("timeout") or 120)

    # ── low level ──────────────────────────────────────────────────────
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            return requests.request(method, self.base_url + path, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise TtsUnreachable(f"breeze-tts at {self.base_url} unreachable "
                                 f"({type(e).__name__}: {e})") from e

    @staticmethod
    def _detail(response: requests.Response) -> str:
        try:
            body = response.json()
            return str(body.get("detail") or body.get("error") or body)[:300]
        except Exception:
            return (response.text or "")[:300]

    def _raise_for(self, response: requests.Response) -> None:
        if response.status_code == 404:
            raise TtsNotFound(f"breeze-tts: {self._detail(response) or 'unknown voice'}", 404)
        if response.status_code == 409:
            raise TtsBusy("breeze-tts: all generation slots are busy", 409)
        raise TtsError(f"breeze-tts HTTP {response.status_code}: "
                       f"{self._detail(response) or response.reason}", response.status_code)

    # ── endpoints ──────────────────────────────────────────────────────
    def health(self) -> dict:
        response = self._request("GET", "/health")
        if response.status_code != 200:
            raise TtsError(f"breeze-tts /health answered {response.status_code}",
                           response.status_code)
        try:
            return response.json()
        except ValueError:
            return {}

    def speech(self, text: str, *, voice: str | None = None,
               instructions: str = "", seed: int | None = None,
               cfg_scale: float | None = None) -> bytes:
        """One utterance -> proper WAV bytes (already re-encoded).

        No `voice` = breeze *designs* a voice from `instructions` (a fixed
        `seed` keeps it near-stable); with `voice`, `instructions` is the
        delivery direction for that cloned voice. 409 retries BUSY_RETRIES
        times; 404 raises TtsNotFound (caller may re-register and retry)."""
        body: dict = {"input": text, "instructions": instructions or "",
                      "response_format": "wav"}
        if voice:
            body["voice"] = voice
        if seed is not None:
            body["seed"] = int(seed)
        if cfg_scale is not None:
            body["cfg_scale"] = float(cfg_scale)
        attempt = 0
        while True:
            response = self._request("POST", "/v1/audio/speech", json=body)
            if response.status_code == 200:
                return reencode_wav(response.content)
            if response.status_code != 409 or attempt >= BUSY_RETRIES:
                self._raise_for(response)
            attempt += 1
            time.sleep(BUSY_BACKOFF * (2 ** (attempt - 1)))

    def register_voice(self, name: str, wav_bytes: bytes, ref_text: str) -> None:
        """Save a clone voice on breeze as <name> (persisted by breeze in its
        voices dir). 409 gets the same retry treatment as speech()."""
        attempt = 0
        while True:
            response = self._request(
                "POST", "/v1/voices",
                data={"name": name, "ref_text": ref_text},
                files={"ref_audio": (name + ".wav", wav_bytes, "audio/wav")})
            if response.status_code in (200, 201):
                return
            if response.status_code != 409 or attempt >= BUSY_RETRIES:
                self._raise_for(response)
            attempt += 1
            time.sleep(BUSY_BACKOFF * (2 ** (attempt - 1)))

    def voices(self) -> list:
        response = self._request("GET", "/v1/voices")
        if response.status_code != 200:
            self._raise_for(response)
        try:
            data = response.json()
        except ValueError:
            return []
        if isinstance(data, dict):  # {"voices": [...]} or {"data": [...]}
            data = data.get("voices") or data.get("data") or []
        return data if isinstance(data, list) else []
