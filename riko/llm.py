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
from pathlib import Path

from openai import OpenAI

from riko.config import ROOT

_client = None
_model = None
_lock = threading.Lock()  # one turn at a time touches the history file

# Reasoning models sometimes leave their thinking inline instead of in a separate field.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")


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
    path = Path(str(cfg["history_file"]))
    return path if path.is_absolute() else ROOT / path


def _load(cfg):
    path = _history_path(cfg)
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):  # a half-written file should not end the chat
        print(f"[llm] {path.name} is damaged, starting a fresh chat")
        return []
    return history if isinstance(history, list) else []


def clean(text):
    """Strip inline <think> blocks and whitespace from a model reply."""
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # an opening tag the server already ate
        text = text.split("</think>", 1)[1]
    text = _BLANK_RUN.sub("\n\n", text.strip())  # reasoning models love trailing blank lines
    return text.strip()


def _complete(client, model, messages, temperature, max_tokens):
    """Ask the model, streaming when the server supports it: reasoning models keep the
    connection quiet while they think, and stream chunks do not expire the read timeout
    the way one long non-stream body does."""
    try:
        stream = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, stream=True,
        )
        parts = []
        finish = None
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
            if chunk.choices:
                finish = chunk.choices[0].finish_reason or finish
        if finish is None:  # the stream died mid-reply; do not save half a sentence
            raise RuntimeError("the LLM closed the stream before finishing its reply")
        return "".join(parts)
    except RuntimeError:
        raise
    except Exception as e:  # server without streaming support (or a transport hiccup): try once, plain
        print(f"[llm] streaming failed ({type(e).__name__}: {e}); retrying without stream")
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
        )
        return response.choices[0].message.content


def reply(user_text, cfg):
    llm = cfg["llm"]
    with _lock:  # connect inside the lock too: two threads must not race to build the client
        if _client is None:
            _connect(cfg)
        history = _load(cfg)
        history.append({"role": "user", "content": user_text})
        max_history = int(llm.get("max_history") or 40)  # None/0 in the yaml should not mean "everything"
        recent = history[-max(1, max_history):]

        raw = _complete(
            _client, _model,
            [{"role": "system", "content": cfg["system_prompt"]}, *recent],
            llm.get("temperature", 0.8),
            llm.get("max_tokens", 1024),
        )
        answer = clean(raw)
        if not answer:
            answer = "...sorry senpai, I lost my train of thought."

        history.append({"role": "assistant", "content": answer})
        path = _history_path(cfg)
        tmp = path.with_suffix(path.suffix + ".tmp")  # write-then-replace: a crash cannot leave half a file
        tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
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
