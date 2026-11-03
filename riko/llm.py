"""Her brain: send the chat so far to an OpenAI-compatible server and get her reply.

Works with any server that speaks /v1/chat/completions: vLLM, llama.cpp,
LM Studio, Ollama, OpenAI itself. Set llm.base_url in character_config.yaml.

The chat is saved to history_file after every turn, so she remembers it
next time. Delete that file (or call forget()) to start fresh.
"""
import json
import os
import re
import sys
import threading

from openai import OpenAI

from riko.config import ROOT

_client = None
_model = None
_lock = threading.Lock()  # one turn at a time touches the history file

# Reasoning models sometimes leave their thinking inline instead of in a separate field.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _connect(cfg):
    global _client, _model
    llm = cfg["llm"]
    key = os.getenv(llm.get("api_key_env") or "", "") or "not-needed"
    _client = OpenAI(base_url=llm["base_url"], api_key=key, timeout=120)
    _model = llm.get("model") or "auto"
    if _model == "auto":
        models = [m.id for m in _client.models.list()]
        if not models:
            raise RuntimeError(f"{llm['base_url']} lists no models; set llm.model in character_config.yaml")
        _model = models[0]
    return _model


def check(cfg):
    """Make sure the server answers, and pick the model. Exits with a hint if it doesn't."""
    try:
        model = _connect(cfg)
    except Exception as e:
        sys.exit(f"[setup] Can't reach the LLM at {cfg['llm']['base_url']}: {type(e).__name__}: {e}\n"
                 f"        Check llm.base_url in character_config.yaml and "
                 f"{cfg['llm'].get('api_key_env') or 'the API key'} in .env.")
    print(f"[setup] LLM: {model} at {cfg['llm']['base_url']}")


def model_name():
    return _model


def _history_path(cfg):
    return ROOT / cfg["history_file"]


def _load(cfg):
    path = _history_path(cfg)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def clean(text):
    """Strip inline <think> blocks and whitespace from a model reply."""
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # an opening tag the server already ate
        text = text.split("</think>", 1)[1]
    return text.strip()


def reply(user_text, cfg):
    if _client is None:
        _connect(cfg)
    llm = cfg["llm"]
    with _lock:
        history = _load(cfg)
        history.append({"role": "user", "content": user_text})
        recent = history[-int(llm.get("max_history", 40)):]

        response = _client.chat.completions.create(
            model=_model,
            messages=[{"role": "system", "content": cfg["system_prompt"]}, *recent],
            temperature=llm.get("temperature", 0.8),
            max_tokens=llm.get("max_tokens", 1024),
        )
        answer = clean(response.choices[0].message.content)
        if not answer:
            answer = "...sorry senpai, I lost my train of thought."

        history.append({"role": "assistant", "content": answer})
        _history_path(cfg).write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return answer


def forget(cfg):
    """Wipe the chat history."""
    with _lock:
        _history_path(cfg).unlink(missing_ok=True)


if __name__ == "__main__":
    # One test message: uv run python -m riko.llm
    from riko.config import load_config
    cfg = load_config()
    check(cfg)
    print("Riko:", reply("Hi! Say something short.", cfg))
