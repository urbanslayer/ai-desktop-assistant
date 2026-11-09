"""Tests for hub/brain.py: the ported LLM logic against a fake OpenAI server.

The fake server mirrors tests/conftest.py's FakeLLMServer (which stays the
property of the root test suite); hub tests keep their own copy so they never
depend on riko/ internals. The thinking tag is built with chr() because
angle-bracket token filters can eat literal copies of it in editors.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hub import brain as brain_mod

REPLY = "Nice to meet you, senpai!"
OPEN = chr(60) + "think" + chr(62)        # opening thinking tag
CLOSE = chr(60) + "/" + "think" + chr(62)  # closing thinking tag


class FakeHubLLM:
    """Minimal stand-in for vLLM/llama.cpp: /v1/models + /v1/chat/completions (json & SSE)."""

    def __init__(self, reply=REPLY):
        self.reply = reply
        self.stream_ok = True      # False -> 400 on stream:true, like a completion-only server
        self.model_id = "test-model"
        self.chat_requests = []    # parsed JSON body of every chat request received
        self.models_calls = 0      # GET /v1/models count (auto-resolution traffic)
        self.auth_headers = []     # Authorization header of every request received
        self.fail = False          # True -> 500 on every chat request
        # requests whose max_tokens < truncate_below get only hidden reasoning,
        # empty content and finish_reason='length', like a reasoning model starved of tokens.
        self.truncate_below = None
        # list of {content, finish, tool_call: (name, arguments)} — one canned
        # streamed reply per chat request; the LAST entry repeats, so a
        # runaway retry loop cannot hang a test that caps at one retry.
        self.script = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.auth_headers.append(self.headers.get("Authorization") or "")
                if self.path == "/v1/models":
                    outer.models_calls += 1
                    return self._send({"object": "list", "data": [
                        {"id": outer.model_id, "object": "model", "owned_by": "fake"}]})
                self._send({"error": "not found"}, 404)

            def _truncated(self, body):
                limit = outer.truncate_below
                return limit is not None and int(body.get("max_tokens") or 0) < limit

            def do_POST(self):
                outer.auth_headers.append(self.headers.get("Authorization") or "")
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/v1/chat/completions":
                    return self._send({"error": "not found"}, 404)
                outer.chat_requests.append(body)
                if outer.fail:
                    return self._send({"error": {"message": "boom"}}, 500)
                entry = outer._next_script()
                if body.get("stream"):
                    if not outer.stream_ok:
                        return self._send({"error": {"message": "streaming not supported"}}, 400)
                    return self._sse(body, entry)
                if entry is not None:  # scripted answer, non-streaming shape
                    msg = {"role": "assistant", "content": entry.get("content") or ""}
                    if entry.get("tool_call"):
                        name, arguments = entry["tool_call"]
                        msg["tool_calls"] = [{"id": "call_fake", "type": "function",
                                              "function": {"name": name,
                                                          "arguments": arguments}}]
                    return self._send({
                        "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
                        "model": outer.model_id,
                        "choices": [{"index": 0,
                                     "finish_reason": entry.get("finish", "stop"),
                                     "message": msg}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2},
                    })
                if self._truncated(body):  # all budget on thinking, nothing left to say
                    return self._send({
                        "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
                        "model": outer.model_id,
                        "choices": [{"index": 0, "finish_reason": "length",
                                     "message": {"role": "assistant", "content": "",
                                                 "reasoning_content": "hmm let me think"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    })
                return self._send({
                    "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
                    "model": outer.model_id,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": outer.reply}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

            def _sse(self, body, entry=None):
                if entry is not None:  # scripted reply: content, finish, optional tool call
                    content = entry.get("content") or ""
                    finish = entry.get("finish", "stop")
                    deltas = [{"role": "assistant"}]
                    if content:
                        half = max(1, len(content) // 2)
                        deltas += [{"content": content[:half]}, {"content": content[half:]}]
                    if entry.get("tool_call"):
                        name, arguments = entry["tool_call"]
                        deltas.append({"tool_calls": [{
                            "index": 0, "id": "call_fake", "type": "function",
                            "function": {"name": name, "arguments": arguments}}]})
                    deltas.append({})
                elif self._truncated(body):  # thinking deltas, zero content, finish='length'
                    deltas = [{"role": "assistant"}, {"reasoning_content": "hmm " * 20}, {}]
                    finish = "length"
                else:
                    half = max(1, len(outer.reply) // 2)
                    deltas = [{"role": "assistant"},
                              {"content": outer.reply[:half]},
                              {"content": outer.reply[half:]},
                              {}]
                    finish = "stop"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b": ping\n\n")
                for i, delta in enumerate(deltas):
                    payload = {
                        "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0,
                        "model": outer.model_id,
                        "choices": [{"index": 0, "delta": delta,
                                     "finish_reason": finish if i == len(deltas) - 1 else None}],
                    }
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def _next_script(self):
        if not self.script:
            return None
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_llm():
    server = FakeHubLLM()
    yield server
    server.stop()


def llm_settings(fake_llm, **over):
    s = {"base_url": fake_llm.base_url, "api_key": "", "model": "auto",
         "temperature": 0.8, "max_tokens": 1024}
    s.update(over)
    return s


def chat(fake_llm, brain=None, text="Hi Riko!", **over):
    brain = brain or brain_mod.Brain()
    return brain.chat(llm_settings(fake_llm, **over), [{"role": "user", "content": text}]), brain


# ── clean(): thinking-block stripping (ported from tests/test_llm.py) ──────

def test_clean_strips_think_blocks():
    assert brain_mod.clean(f"{OPEN}\nlots of hidden reasoning\n{CLOSE}Hello senpai!") == "Hello senpai!"
    assert brain_mod.clean("no tags here") == "no tags here"
    assert brain_mod.clean("") == ""
    assert brain_mod.clean(None) == ""


def test_clean_edge_cases():
    assert brain_mod.clean(f"reasoning was here{CLOSE}real answer") == "real answer"
    assert brain_mod.clean("still thinking") == "still thinking"
    assert (brain_mod.clean(f"<THINK>a{CLOSE}b<c:d>x\ny</C:D>z")
            == f"b<c:d>x\ny</C:D>z")


def test_clean_removes_blank_line_runs():
    assert brain_mod.clean("a\n\n\n\n\nb") == "a\n\nb"
    assert brain_mod.clean("  \n\na\n\n\nb  ") == "a\n\nb"
    assert brain_mod.clean("a\n\nb") == "a\n\nb"  # one blank line is kept


# ── model resolution ────────────────────────────────────────────────────────

def test_auto_picks_first_model_from_server(fake_llm):
    answer, _ = chat(fake_llm)
    assert answer == REPLY
    assert fake_llm.chat_requests[-1]["model"] == "test-model"  # auto-discovered via /v1/models


def test_explicit_model_skips_models_list(fake_llm):
    answer, _ = chat(fake_llm, text="hi", model="my-model")
    assert answer == REPLY
    assert fake_llm.chat_requests[-1]["model"] == "my-model"
    assert fake_llm.models_calls == 0


def test_auto_resolution_cached_and_invalidated(fake_llm):
    b = brain_mod.Brain()
    settings = llm_settings(fake_llm)
    b.chat(settings, [{"role": "user", "content": "one"}])
    b.chat(settings, [{"role": "user", "content": "two"}])
    assert fake_llm.models_calls == 1  # second chat reused the cache
    b.invalidate()  # settings changed -> must re-resolve
    b.chat(settings, [{"role": "user", "content": "three"}])
    assert fake_llm.models_calls == 2


# ── completion behaviour (ported from tests/test_llm.py) ────────────────────

def test_chat_streams_by_default(fake_llm):
    answer, _ = chat(fake_llm, text="Stream for me")
    assert answer == REPLY
    assert fake_llm.chat_requests[-1].get("stream") is True


def test_chat_falls_back_to_non_streaming(fake_llm):
    fake_llm.stream_ok = False  # server 400s on stream:true, like a completion-only server
    answer, _ = chat(fake_llm)
    assert answer == REPLY
    assert fake_llm.chat_requests[-1].get("stream") is not True


def test_chat_strips_think_from_model_output(fake_llm):
    fake_llm.reply = f"{OPEN}hidden reasoning here{CLOSE}\n\nThe answer is two, senpai."
    answer, _ = chat(fake_llm, text="2+2?")
    assert answer == "The answer is two, senpai."


def test_length_truncation_retries_with_double_max_tokens(fake_llm):
    fake_llm.truncate_below = 2048  # first 1024-token attempt gets only hidden thinking
    answer, _ = chat(fake_llm)
    assert answer == REPLY
    assert [r["max_tokens"] for r in fake_llm.chat_requests] == [1024, 2048]


def test_retry_also_empty_falls_back_and_stops(fake_llm):
    fake_llm.truncate_below = 10 ** 9  # every budget gets truncated
    answer, _ = chat(fake_llm)
    assert answer.startswith("...sorry senpai")
    assert [r["max_tokens"] for r in fake_llm.chat_requests] == [1024, 2048]


def test_retry_capped_at_16384(fake_llm):
    fake_llm.truncate_below = 10 ** 9
    answer, _ = chat(fake_llm, max_tokens=16384)  # doubling would exceed the cap
    assert answer.startswith("...sorry senpai")
    assert [r["max_tokens"] for r in fake_llm.chat_requests] == [16384]  # no point retrying


def test_default_max_tokens_is_4096(fake_llm):
    s = {"base_url": fake_llm.base_url, "model": "auto"}  # max_tokens key left out
    brain_mod.Brain().chat(s, [{"role": "user", "content": "hi"}])
    assert fake_llm.chat_requests[-1]["max_tokens"] == 4096
    assert brain_mod.LLM_DEFAULTS["max_tokens"] == 4096


def test_reasoning_effort_and_extra_body_reach_the_server(fake_llm):
    _, _ = chat(fake_llm, reasoning_effort="low",
                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    req = fake_llm.chat_requests[-1]
    assert req["reasoning_effort"] == "low"
    assert req["chat_template_kwargs"] == {"enable_thinking": False}  # extra_body merged in


def test_passthrough_survives_non_streaming_fallback(fake_llm):
    fake_llm.stream_ok = False
    _, _ = chat(fake_llm, reasoning_effort="minimal", extra_body={"top_k": 5})
    req = fake_llm.chat_requests[-1]
    assert req.get("stream") is not True
    assert req["reasoning_effort"] == "minimal"
    assert req["top_k"] == 5


def test_no_passthrough_keys_omitted_from_body(fake_llm):
    _, _ = chat(fake_llm)
    assert "reasoning_effort" not in fake_llm.chat_requests[-1]
    assert "chat_template_kwargs" not in fake_llm.chat_requests[-1]


def test_empty_model_answer_gets_placeholder(fake_llm):
    fake_llm.reply = "   "
    answer, _ = chat(fake_llm)
    assert answer.startswith("...sorry senpai")


def test_server_failure_raises(fake_llm):
    fake_llm.fail = True
    with pytest.raises(Exception):
        chat(fake_llm)


def test_chat_without_base_url_raises():
    with pytest.raises(RuntimeError, match="LLM not configured"):
        brain_mod.Brain().chat({"base_url": ""}, [{"role": "user", "content": "hi"}])


# ── cut-off reply guard (docs/API.md "Cut-off reply guard") ─────────────────

def test_cutoff_stop_fragment_gets_one_retry(fake_llm):
    # live failure: thinking off -> stream ends with finish 'stop' after
    # a couple of words and the hub used to store/speak the fragment
    fake_llm.script = [{"content": "Oh! Then", "finish": "stop"},
                       {"content": "Oh! Then you must be new here, senpai.", "finish": "stop"}]
    answer, _ = chat(fake_llm)
    assert answer == "Oh! Then you must be new here, senpai."
    assert len(fake_llm.chat_requests) == 2


def test_complete_reply_never_retried(fake_llm):
    fake_llm.script = [{"content": "Done!", "finish": "stop"}]  # short but complete
    answer, _ = chat(fake_llm)
    assert answer == "Done!"
    assert len(fake_llm.chat_requests) == 1


def test_empty_reply_gets_one_retry(fake_llm):
    fake_llm.script = [{"content": "", "finish": "stop"},
                       {"content": "Sorry, I drifted — what were we doing?", "finish": "stop"}]
    answer, _ = chat(fake_llm)
    assert answer == "Sorry, I drifted — what were we doing?"
    assert len(fake_llm.chat_requests) == 2


def test_length_truncation_mid_sentence_gets_one_retry(fake_llm):
    # long enough to dodge the short-reply rule; finish='length' says the
    # token budget died mid-sentence, so it is still a cut-off
    fake_llm.script = [{"content": "The capital of France is actually a remarkably",
                        "finish": "length"},
                       {"content": "The capital of France is Paris.", "finish": "stop"}]
    answer, _ = chat(fake_llm)
    assert answer == "The capital of France is Paris."
    assert len(fake_llm.chat_requests) == 2


def test_long_complete_stop_reply_not_retried(fake_llm):
    fake_llm.script = [{"content": "Paris is the capital of France, senpai.",
                        "finish": "stop"}]
    answer, _ = chat(fake_llm)
    assert answer == "Paris is the capital of France, senpai."
    assert len(fake_llm.chat_requests) == 1


def test_tool_call_turn_never_retried(fake_llm):
    # 'Let me check that.' reads like a fragment with no tools offered, but
    # the turn DID call a tool: the guard must not touch it
    fake_llm.script = [{"content": "Let me check that.", "finish": "tool_calls",
                        "tool_call": ("web_search", '{"q": "x"}')}]
    tools = [{"type": "function", "function": {"name": "web_search",
                                               "parameters": {"type": "object"}}}]
    out = brain_mod.Brain().chat_tools(llm_settings(fake_llm),
                                       [{"role": "user", "content": "hi"}], tools)
    assert out["tool_calls"] and out["tool_calls"][0]["name"] == "web_search"
    assert len(fake_llm.chat_requests) == 1


def test_cutoff_retry_drops_enable_thinking_keeps_the_rest(fake_llm):
    fake_llm.script = [{"content": "Right away", "finish": "stop"},
                       {"content": "Right away, here is your weather, senpai!",
                        "finish": "stop"}]
    answer, _ = chat(fake_llm, reasoning_effort="low",
                     extra_body={"chat_template_kwargs": {"enable_thinking": False},
                                 "top_k": 5})
    assert answer == "Right away, here is your weather, senpai!"
    first, second = fake_llm.chat_requests
    assert first["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in second      # the retry may think again
    assert second["top_k"] == 5                      # every other key survives
    assert second["reasoning_effort"] == "low"       # reasoning_effort kept


def test_cutoff_retry_worse_than_original_keeps_original(fake_llm):
    fake_llm.script = [{"content": "Oh! Then", "finish": "stop"},
                       {"content": "Hmm", "finish": "stop"}]
    answer, _ = chat(fake_llm)
    assert answer == "Oh! Then"  # keep the longer of the two
    assert len(fake_llm.chat_requests) == 2


def test_cutoff_retry_cap_is_one(fake_llm):
    fake_llm.script = [{"content": "Oh", "finish": "stop"},
                       {"content": "Then", "finish": "stop"}]
    chat(fake_llm)
    assert len(fake_llm.chat_requests) == 2  # never a third attempt


def test_still_empty_after_retry_falls_back_to_placeholder(fake_llm):
    fake_llm.script = [{"content": "", "finish": "stop"},
                       {"content": "   ", "finish": "stop"}]
    answer, _ = chat(fake_llm)
    assert answer.startswith("...sorry senpai")
    assert len(fake_llm.chat_requests) == 2


def test_only_emotion_tag_reply_retries_then_placeholder(fake_llm):
    fake_llm.script = [{"content": "[happy]", "finish": "stop"},
                       {"content": "[happy]", "finish": "stop"}]
    answer, _ = chat(fake_llm)
    # a tag-only reply is an empty reply for the guard (the server strips
    # '[happy]' later anyway); the retry is judged the same way
    assert len(fake_llm.chat_requests) == 2
    assert answer == "[happy]"  # brain keeps the raw text; the server's
                                # EMPTY_REPLY floor turns it into the placeholder


def test_cut_off_unit_rules():
    cut = brain_mod._looks_cut_off
    assert cut("Oh! Then", "stop")
    assert cut("Oh", "stop")
    assert cut("Right away", "stop")
    assert cut("", "stop")
    assert cut("[happy]", "stop")
    assert cut("The capital of France is remarkably", "length")
    assert not cut("Nice to meet you, senpai!", "stop")
    assert not cut('"All done."', "stop")      # closer after terminal punct
    assert not cut("(found it!)", "stop")
    assert not cut("Sure thing, senpai! 🎉", "stop")  # emoji tail closes it
    assert not cut("Here is the link https://example.com/docs/page", "stop")  # >25 chars
    assert not cut("", "length")               # token-retry territory
    assert not cut("Yes", "tool_calls")        # never guard a tool turn
