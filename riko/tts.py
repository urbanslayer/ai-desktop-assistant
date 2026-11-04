"""Her voice: turn her reply into a WAV file.

tts: hub    -> the HUB makes the audio (Breeze-TTS-2, per-persona voices).
               We just fetch the chat result's audio_url; see from_hub_audio().
               Needs a hub with TTS configured (docs/VOICE.md).
tts: none   -> no voice (the desktop app still shows her reply in a bubble)
tts: openai -> any OpenAI-compatible /v1/audio/speech server (Kokoro-FastAPI, speaches, ...)
tts: sovits -> GPT-SoVITS API
tts: fish   -> Fish Audio online

The hub mode is the default: every client hears the same voice with no
TTS setup or keys of its own. The others do TTS on this PC.
"""
import io
import os
import sys

import requests
import soundfile as sf

from riko import hub_client
from riko.config import ROOT, need_key
from riko.speech import speech_text

VOICE_FILE = ROOT / "client" / "audio" / "riko.wav"  # inside client/ so the avatar page can load it


def check(cfg):
    if cfg["tts"] == "none":
        print("[setup] tts: none, she will reply in text only")
        return
    if cfg["tts"] == "hub":
        # Never fatal: a hub whose breeze is down just means bubbles today.
        try:
            status = hub_client.get().tts_status()
        except hub_client.HubError as e:
            print(f"[setup] tts: hub, but the hub won't talk about its voice ({e}); "
                  "her replies may show in bubbles only")
            return
        if status is None:
            print("[setup] tts: hub, but this hub is too old for voice "
                  "(/api/tts/status missing). Update the hub; until then she "
                  "replies in bubbles.")
            return
        if status.get("enabled") and status.get("reachable"):
            print(f"[setup] tts: hub — her voice is ready at {status.get('base_url')} "
                  f"({status.get('voices', 0)} saved voices)")
        else:
            why = "is turned off" if not status.get("enabled") else "cannot reach breeze"
            print(f"[setup] tts: hub, but the hub's TTS {why}; "
                  "her replies will show in bubbles until it's up")
        return
    if cfg["tts"] == "fish":
        need_key("FISH_API_KEY", "tts: fish")
        return
    if cfg["tts"] == "openai":
        url = cfg["openai_tts"]["url"]
        try:
            requests.get(url.rstrip("/") + "/models", timeout=3)  # any answer means it is running
        except (requests.ConnectionError, requests.Timeout):
            sys.exit(f"[setup] No TTS server at {url}. Start it, or set tts: none.")
        print(f"[setup] TTS server is up at {url}")
        return
    ref_audio = ROOT / cfg["sovits"]["ref_audio_path"]
    if not ref_audio.exists():
        sys.exit(f"[setup] Voice sample not found: {ref_audio}")
    try:
        requests.head(cfg["sovits"]["url"], timeout=3)  # any answer means it is running
    except requests.ConnectionError:
        sys.exit(f"[setup] GPT-SoVITS is not running at {cfg['sovits']['url']}. "
                 "Start its API first (python api_v2.py in your GPT-SoVITS folder), or use --tts fish.")
    print(f"[setup] GPT-SoVITS is running at {cfg['sovits']['url']}")


def speak(text, cfg, voice=None, speech=None):
    """Make her say `text` on THIS PC's own TTS. Returns the path of the WAV
    file, or None with tts: none / tts: hub (hub audio comes from the chat
    result instead — see from_hub_audio).

    `voice` overrides the configured voice (the active persona's tts_voice);
    only the openai backend has named voices, others keep their own sample/id.

    `speech` is the hub's cleaned spoken text for this reply (ChatResult
    .speech, docs/API.md). Either way the TTS never sees raw markdown or URLs:
    without `speech` we apply riko/speech.py, the mirror of the hub's rules,
    so the local voices say exactly what the hub voice would.
    """
    if cfg["tts"] in ("none", "hub") or not text.strip():
        return None
    spoken = (speech or "").strip() or speech_text(text)
    if not spoken:
        return None
    if cfg["tts"] == "fish":
        audio = _fish(spoken, cfg["fish"])
    elif cfg["tts"] == "openai":
        audio = _openai(spoken, cfg["openai_tts"], voice=voice)
    else:
        audio = _sovits(spoken, cfg["sovits"])
    return _write(audio, cfg["tts"])


def from_hub_audio(audio_bytes, label="hub"):
    """Take WAV bytes the hub made and put them where the avatar page reads
    her voice. Raises on unreadable audio (session falls back to a bubble)."""
    return _write(audio_bytes, label)


def _write(audio, label):
    # Re-save through soundfile: breeze/Fish WAV headers claim the clip is
    # hours long, and browsers believe it and never fire "ended". This fixes it.
    data, sample_rate = sf.read(io.BytesIO(audio))
    VOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    sf.write(VOICE_FILE, data, sample_rate)
    print(f"[tts] {label}: {len(data) / sample_rate:.1f}s of audio")
    return VOICE_FILE


def _sovits(text, s):
    response = requests.post(s["url"], json={
        "text": text,
        "text_lang": s["text_lang"],
        "ref_audio_path": str(ROOT / s["ref_audio_path"]),  # GPT-SoVITS needs the full path
        "prompt_text": s["prompt_text"],
        "prompt_lang": s["prompt_lang"],
    }, timeout=120)
    response.raise_for_status()
    return response.content


def _openai(text, o, voice=None):
    key = os.getenv(o.get("api_key_env") or "", "")
    response = requests.post(
        o["url"].rstrip("/") + "/audio/speech",
        headers={"Authorization": f"Bearer {key}"} if key else {},
        json={"model": o["model"], "voice": voice or o["voice"], "input": text,
              "response_format": "wav"},
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def _fish(text, f):
    body = {"text": text, "format": "wav"}
    if f["voice_id"]:
        body["reference_id"] = f["voice_id"]
    response = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={"Authorization": f"Bearer {os.getenv('FISH_API_KEY')}", "model": f["model"]},
        json=body,
        timeout=120,
    )
    response.raise_for_status()
    return response.content


if __name__ == "__main__":
    # Test her voice on its own: uv run python -m riko.tts   (add --fish for Fish Audio)
    from riko.config import load_config
    cfg = load_config()
    if "--fish" in sys.argv:
        cfg["tts"] = "fish"
    if cfg["tts"] == "hub":
        hub_client.check(cfg)  # the hub needs to be up before we ask it for audio
        line = "If you can hear this, my voice is set up correctly."
        print("Wrote", from_hub_audio(hub_client.get().tts_audio(line)))
    else:
        check(cfg)
        print("Wrote", speak("If you can hear this, my voice is set up correctly.", cfg))
