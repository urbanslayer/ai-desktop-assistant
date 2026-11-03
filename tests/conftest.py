"""Shared test scaffolding: a fake OpenAI-compatible LLM server and fixtures.

Everything here talks only to 127.0.0.1 and needs no mic, GPU, or internet.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from riko import llm

REPLY = "Nice to meet you, senpai!"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLLMServer:
    """Minimal stand-in for vLLM/llama.cpp: /v1/models + /v1/chat/completions (json & SSE)."""

    def __init__(self, reply=REPLY):
        self.reply = reply
        self.stream_ok = True  # False -> 400 on stream:true, like a completion-only server
        self.model_id = "test-model"
        self.chat_requests = []  # parsed JSON body of every chat request received
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
                if self.path == "/v1/models":
                    return self._send({"object": "list", "data": [
                        {"id": outer.model_id, "object": "model", "owned_by": "fake"}]})
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/v1/chat/completions":
                    return self._send({"error": "not found"}, 404)
                outer.chat_requests.append(body)
                if body.get("stream"):
                    if not outer.stream_ok:
                        return self._send({"error": {"message": "streaming not supported"}}, 400)
                    return self._sse()
                return self._send({
                    "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
                    "model": outer.model_id,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": outer.reply}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

            def _sse(self):
                half = max(1, len(outer.reply) // 2)
                chunks = [
                    {"role": "assistant"},
                    {"content": outer.reply[:half]},
                    {"content": outer.reply[half:]},
                    {},  # final chunk: only finish_reason
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b": ping\n\n")  # SSE comment; a good server may send these
                for i, delta in enumerate(chunks):
                    payload = {
                        "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0,
                        "model": outer.model_id,
                        "choices": [{"index": 0, "delta": delta,
                                     "finish_reason": "stop" if i == len(chunks) - 1 else None}],
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

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_cfg(base_url, history_file, max_history=40):
    return {
        "history_file": str(history_file),
        "system_prompt": "You are Riko, a test character.",
        "llm": {"base_url": base_url, "api_key_env": "TEST_NO_SUCH_KEY", "model": "auto",
                "temperature": 0.8, "max_tokens": 1024, "max_history": max_history},
        "asr": "remote",
        "remote_asr": {},
        "tts": "none",
        "vrm_model": "character_files/sample_avatar.vrm",
        # keep viewer._settings identical no matter which fixture started the viewer last
        "desktop": {"width": 420, "height": 680, "bubble_seconds": 8},
    }


@pytest.fixture
def fake_llm():
    server = FakeLLMServer()
    yield server
    server.stop()


@pytest.fixture
def cfg(fake_llm, tmp_path):
    return make_cfg(fake_llm.base_url, tmp_path / "chat_history.json")


@pytest.fixture(autouse=True)
def clean_llm_globals():
    """llm keeps a module-level client/model; never leak them between tests."""
    llm._client, llm._model = None, None
    yield
    llm._client, llm._model = None, None
