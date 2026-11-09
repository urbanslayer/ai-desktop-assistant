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

# Cut-off reply guard (docs/API.md "Cut-off reply guard"): some template
# builds (live: qwen4exp on vLLM with chat_template_kwargs.enable_thinking
# = false) end the stream after a couple of words with finish_reason
# 'stop'. A reply this short without terminal punctuation is the cut-off,
# not the answer.
_CUT_OFF_WORDS = 4
_CUT_OFF_CHARS = 25
_TERMINAL = ".!?…"
_CLOSERS = "\"'“”’«»)]}"
_VARIATION = "\ufe0f\u200d\u20e3"
# leading [happy]-style tags (emotion + action) are not part of the words
# we count; a bracket token right before '(' / '[' is a markdown link, keep it
_LEAD_TAG = re.compile(r"\s*[\[({<][A-Za-z][A-Za-z _-]{0,23}[\])}>](?![\(\[])")

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


def _strip_lead_tags(text: str) -> str:
    """Drop leading [happy]/[wave]-style tags before judging a reply: the
    server strips them later anyway, so '[happy]' alone IS an empty reply."""
    while True:
        m = _LEAD_TAG.match(text)
        if not m:
            return text.strip()
        text = text[m.end():]


def _ends_emoji(text: str) -> bool:
    t = text.rstrip(_VARIATION)
    if not t:
        return False
    cp = ord(t[-1])
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF


def _ends_complete(text: str) -> bool:
    """Does the reply end like a finished thought: terminal punctuation
    (. ! ? …), optionally followed by closing quotes/brackets ('Done!'
    '"Done!"' '(done!)'), or a closing emoji?"""
    t = (text or "").rstrip()
    if not t:
        return False
    if _ends_emoji(t):
        return True
    core = t.rstrip(_CLOSERS) or t
    return bool(core) and core[-1] in _TERMINAL


def _looks_cut_off(text: str, finish: str | None) -> bool:
    """Is this final, tool-call-free reply a stream that died mid-sentence?
    `text` must already be clean()."""
    probe = _strip_lead_tags(text)
    if not probe:
        # empty / whitespace-only, or the reply was nothing but tags; an
        # empty 'length' reply is the token-doubling retry's territory above
        return finish != "length"
    if finish not in ("stop", "length"):
        return False
    if _ends_complete(probe):
        return False
    if finish == "length":
        return True  # the token budget died mid-sentence, whatever its size
    return len(probe.split()) < _CUT_OFF_WORDS or len(probe) < _CUT_OFF_CHARS


def _retry_kwargs(kwargs: dict) -> tuple[dict, bool]:
    """Same kwargs, minus an explicit thinking-off switch: a cut-off stream
    with enable_thinking=false usually means the template cut into the
    answer, so the retry lets the model think again. reasoning_effort and
    every other passthrough key stay. Returns (kwargs, dropped_thinking)."""
    eb = kwargs.get("extra_body")
    if not isinstance(eb, dict):
        return kwargs, False
    ctk = eb.get("chat_template_kwargs")
    if not (isinstance(ctk, dict) and ctk.get("enable_thinking") is False):
        return kwargs, False
    eb2 = dict(eb)
    ctk2 = {k: v for k, v in ctk.items() if k != "enable_thinking"}
    if ctk2:
        eb2["chat_template_kwargs"] = ctk2
    else:
        eb2.pop("chat_template_kwargs", None)
    kw2 = dict(kwargs)
    kw2["extra_body"] = eb2
    return kw2, True


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
    finish_reason 'length' -> one retry at double the budget, capped; plus
    the cut-off reply guard: a final answer (no tool calls) that looks like
    a stream that died mid-sentence gets ONE retry on the same messages.
    The two share one retry budget: at most one second attempt per call."""
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
    retried = False
    if not text.strip() and not tool_calls and finish == "length":
        retry = min(max_tokens * 2, _MAX_TOKENS_RETRY_CAP)
        if retry > max_tokens:
            retried = True
            print(f"[hub-brain] ran out of tokens while thinking; retrying with {retry}")
            text, finish, thought, tool_calls = _attempt(
                client, model, messages, temperature, retry, kwargs, bool(tools))
        if not text.strip():
            print(f"[hub-brain] the reply stayed empty after every token went to "
                  f"{'hidden thinking' if thought else 'generation'}: raise llm.max_tokens "
                  "in the hub settings, set llm.reasoning_effort, or disable thinking, "
                  "e.g. llm.extra_body: {chat_template_kwargs: {enable_thinking: false}} "
                  "for Qwen on vLLM/llama.cpp")
    if not tool_calls and not retried:
        probe = clean(text)
        if _looks_cut_off(probe, finish):
            print(f"[hub-brain] reply looked cut off ({len(probe)} chars, "
                  f"finish={finish}); retrying once")
            retry_kwargs, dropped = _retry_kwargs(kwargs)
            if dropped:
                print("[hub-brain] cut-off retry: dropped enable_thinking=false, "
                      "this attempt may think")
            r_text, r_finish, r_thought, r_calls = _attempt(
                client, model, messages, temperature, max_tokens,
                retry_kwargs, bool(tools))
            if r_calls:  # the retry found a tool to call: always the better turn
                return r_text, r_calls
            r_probe = clean(r_text)
            # take the retry when it reads complete or says more than the
            # fragment did; otherwise keep the longer of the two (the
            # fragment — an empty retry can never be the longer one)
            if r_probe and (_ends_complete(_strip_lead_tags(r_probe))
                            or len(r_probe) > len(probe)):
                text = r_text
    # both attempts cut off with nothing to show -> '' (the caller's
    # EMPTY_REPLY placeholder takes over)
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
