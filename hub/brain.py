"""Her brain: talk to any OpenAI-compatible server (/v1/chat/completions).

A port of riko/llm.py for the hub: same thinking-block stripping, same
streaming-first request with a non-streaming fallback, same reasoning-model
retry (empty reply + finish_reason 'length' -> one retry at double the token
budget, capped at 16384), and model 'auto' = first id from /v1/models.

Unlike the old module this is stateless per call: it takes a settings dict
(already merged with defaults by the server) and a ready-made message list,
and it does not touch history files — the hub store owns those.
"""
import re
import threading

from openai import OpenAI

# Reasoning models sometimes leave their thinking inline instead of in a separate field.
_THINK = re.compile(r"<think.*?</think>", re.DOTALL | re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")

_MAX_TOKENS_RETRY_CAP = 16384  # the one doubling retry stops here

LLM_DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "model": "auto",
    "temperature": 0.8,
    "max_tokens": 4096,       # reasoning models burn this on hidden thinking
    "max_history": 40,
    "reasoning_effort": "",    # omitted from the request when empty
    "extra_body": {},          # vendor-specific passthrough fields
}

DEFAULT_SWITCH_PHRASES = ["switch to", "change to", "become", "persona", "talk to",
                          "i want to talk to"]

EMPTY_REPLY = "...sorry senpai, I lost my train of thought."


class ToolsRejected(Exception):
    """The server 400'd the `tools` parameter itself (not the prompt): the
    model/server build cannot do function calling. The caller retries the
    turn without tools and marks it in /api/tools/status."""


def _looks_like_tools_rejection(e: Exception) -> bool:
    if getattr(e, "status_code", None) != 400:
        return False
    text = str(e).lower()
    return "tool" in text or "function" in text


def clean(text):
    """Strip inline think-blocks and whitespace from a model reply."""
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # an opening tag the server already ate
        text = text.split("</think>", 1)[1]
    text = _BLANK_RUN.sub("\n\n", text.strip())  # reasoning models love trailing blank lines
    return text.strip()


def _finalize_calls(calls: dict) -> list[dict]:
    """Collected streaming deltas -> [{id, name, arguments}] in index order."""
    out = []
    for i, idx in enumerate(sorted(calls)):
        slot = calls[idx]
        out.append({"id": slot["id"] or f"call_{i}", "name": slot["name"],
                    "arguments": slot["arguments"] or ""})
    return out


def _attempt(client, model, messages, temperature, budget, kwargs, want_tools):
    """One completion attempt: streaming when the server supports it.

    Returns (text, finish_reason, thought, tool_calls). reasoning models keep
    the connection quiet while they think, and stream chunks do not expire
    the read timeout the way one long non-stream body does; tool_calls
    arrive as per-index deltas and are stitched here."""
    try:
        stream = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
            max_tokens=budget, stream=True, **kwargs,
        )
        parts = []
        calls: dict = {}
        finish = None
        thought = False
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta:
                delta = chunk.choices[0].delta
                if delta.content:
                    parts.append(delta.content)
                # vLLM/llama.cpp/Ollama put hidden thinking here, not in content
                if getattr(delta, "reasoning_content", None):
                    thought = True
                for tc in (getattr(delta, "tool_calls", None) or []):
                    slot = calls.setdefault(tc.index or 0,
                                            {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if fn.name:
                            slot["name"] += fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments
            if chunk.choices:
                finish = chunk.choices[0].finish_reason or finish
        if finish is None:  # the stream died mid-reply; do not save half a sentence
            raise RuntimeError("the LLM closed the stream before finishing its reply")
        return "".join(parts), finish, thought, _finalize_calls(calls)
    except RuntimeError:
        raise
    except Exception as e:  # server without streaming support (or a transport hiccup): try once, plain
        print(f"[hub-brain] streaming failed ({type(e).__name__}: {e}); retrying without stream")
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=budget, **kwargs,
            )
        except Exception as e2:
            if want_tools and (_looks_like_tools_rejection(e2)
                               or _looks_like_tools_rejection(e)):
                raise ToolsRejected(f"{type(e2).__name__}: {e2}") from e2
            raise
        message = response.choices[0].message
        text = message.content or ""
        thought = bool(getattr(message, "reasoning_content", None))
        tool_calls = [{"id": tc.id or f"call_{i}", "name": tc.function.name or "",
                       "arguments": tc.function.arguments or ""}
                      for i, tc in enumerate(message.tool_calls or [])]
        return text, response.choices[0].finish_reason, thought, tool_calls


def _run(client, model, messages, temperature, max_tokens,
         reasoning_effort=None, extra_body=None, tools=None):
    """A completion with the reasoning-model token retry: empty reply +
    finish_reason 'length' -> one retry at double the budget, capped."""
    kwargs = {}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if extra_body:
        kwargs["extra_body"] = dict(extra_body)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    text, finish, thought, tool_calls = _attempt(
        client, model, messages, temperature, max_tokens, kwargs, bool(tools))
    if not text.strip() and not tool_calls and finish == "length":
        retry = min(max_tokens * 2, _MAX_TOKENS_RETRY_CAP)
        if retry > max_tokens:
            print(f"[hub-brain] ran out of tokens while thinking; retrying with {retry}")
            text, finish, thought, tool_calls = _attempt(
                client, model, messages, temperature, retry, kwargs, bool(tools))
        if not text.strip():
            print(f"[hub-brain] the reply stayed empty after every token went to "
                  f"{'hidden thinking' if thought else 'generation'}: raise llm.max_tokens "
                  "in the hub settings, set llm.reasoning_effort, or disable thinking, "
                  "e.g. llm.extra_body: {chat_template_kwargs: {enable_thinking: false}} "
                  "for Qwen on vLLM/llama.cpp")
    return text, tool_calls


def _complete(client, model, messages, temperature, max_tokens,
              reasoning_effort=None, extra_body=None):
    """Ask the model one plain question; returns the raw text (thinking
    blocks may still be inline — clean() strips them later)."""
    text, _calls = _run(client, model, messages, temperature, max_tokens,
                        reasoning_effort=reasoning_effort, extra_body=extra_body)
    return text


class Brain:
    """One Brain per hub app. Resolves model 'auto' against the server's /v1/models
    and caches the answer until the LLM settings change (settings PUT calls
    invalidate()); everything else is a per-call OpenAI client."""

    def __init__(self):
        self._cache = {}  # (base_url, api_key) -> resolved model id
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    def cached_model(self, settings: dict) -> str | None:
        """What we believe the model is right now, without touching the network:
        the resolved 'auto' id if we have one, the explicit model, else None."""
        model = settings.get("model") or "auto"
        if model != "auto":
            return model
        with self._lock:
            return self._cache.get((settings.get("base_url", ""), settings.get("api_key", "")))

    def _resolve_model(self, client: OpenAI, settings: dict) -> str:
        model = settings.get("model") or "auto"
        if model != "auto":
            return model
        key = (settings.get("base_url", ""), settings.get("api_key", ""))
        with self._lock:
            hit = self._cache.get(key)
        if hit:
            return hit
        models = [m.id for m in client.models.list()]
        if not models:
            raise RuntimeError(f"{settings.get('base_url')} lists no models; set the model in the hub settings")
        with self._lock:
            self._cache[key] = models[0]
        return models[0]

    def test(self, settings: dict) -> str:
        """Cheap reachability/auth check for POST /api/settings/test: always
        hits /v1/models over the network (bad base_url/key surface there),
        resolves 'auto' and refreshes the cache. Returns the model that would
        be used; raises on any transport/auth failure."""
        base_url = settings.get("base_url") or ""
        if not base_url:
            raise RuntimeError("LLM not configured")
        client = OpenAI(base_url=base_url, api_key=_api_key_arg(settings), timeout=30)
        models = [m.id for m in client.models.list()]
        model = settings.get("model") or "auto"
        if model == "auto":
            if not models:
                raise RuntimeError(f"{base_url} lists no models; set the model in the hub settings")
            model = models[0]
            with self._lock:
                self._cache[(base_url, settings.get("api_key", ""))] = model
        return model

    def chat(self, settings: dict, messages: list[dict]) -> str:
        """One model turn. `settings` is the merged llm settings object; `messages`
        is the full list including the leading system prompt. Raises on any
        transport/model failure; returns the cleaned reply (never empty)."""
        base_url = settings.get("base_url") or ""
        if not base_url:
            raise RuntimeError("LLM not configured")
        client = OpenAI(base_url=base_url, api_key=_api_key_arg(settings), timeout=120)
        model = self._resolve_model(client, settings)
        raw = _complete(
            client, model, messages,
            float(settings.get("temperature", 0.8)),
            int(settings.get("max_tokens", 4096) or 4096),
            reasoning_effort=settings.get("reasoning_effort") or None,
            extra_body=settings.get("extra_body") or None,
        )
        answer = clean(raw)
        return answer or EMPTY_REPLY

    def chat_tools(self, settings: dict, messages: list[dict],
                   tools: list[dict]) -> dict:
        """One model step WITH function calling. Returns
        {text: <cleaned, may be empty>, tool_calls: [{id,name,arguments}]}.
        Raises ToolsRejected when the server 400s the `tools` parameter
        itself; any other transport/model failure raises as usual."""
        base_url = settings.get("base_url") or ""
        if not base_url:
            raise RuntimeError("LLM not configured")
        client = OpenAI(base_url=base_url, api_key=_api_key_arg(settings), timeout=120)
        model = self._resolve_model(client, settings)
        raw, tool_calls = _run(
            client, model, messages,
            float(settings.get("temperature", 0.8)),
            int(settings.get("max_tokens", 4096) or 4096),
            reasoning_effort=settings.get("reasoning_effort") or None,
            extra_body=settings.get("extra_body") or None,
            tools=tools,
        )
        return {"text": clean(raw), "tool_calls": tool_calls}


def _api_key_arg(settings: dict) -> str:
    # An OpenAI-compatible server that needs no key still wants *a* key string.
    return settings.get("api_key") or "not-needed"
