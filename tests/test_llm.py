"""Tests for riko.llm: inline thinking-block cleanup and chat vs a fake OpenAI server.

The thinking tag is built with chr() because angle-bracket token filters can eat
literal copies of it in editors/transport layers; the code under test uses it raw.
"""
import json
from pathlib import Path

from conftest import REPLY
from riko import llm

OPEN = chr(60) + "think" + chr(62)      # opening thinking tag
CLOSE = chr(60) + "/" + "think" + chr(62)  # closing thinking tag


def test_clean_strips_think_blocks():
    assert llm.clean(f"{OPEN}\nlots of hidden reasoning\n{CLOSE}Hello senpai!") == "Hello senpai!"
    assert llm.clean("no tags here") == "no tags here"
    assert llm.clean("") == ""
    assert llm.clean(None) == ""


def test_clean_edge_cases():
    # a server that already ate the opening tag: keep what follows the close tag
    assert llm.clean(f"reasoning was here{CLOSE}real answer") == "real answer"
    # only an opening tag: the reply was cut off mid-thought, keep the text rather than lose it
    assert llm.clean("still thinking") == "still thinking"
    # case-insensitive, multiple blocks, other tags untouched
    assert (llm.clean(f"<THINK>a{CLOSE}b<c:d>x\ny</C:D>z")
            == f"b<c:d>x\ny</C:D>z")


def test_clean_removes_blank_line_runs():
    assert llm.clean("a\n\n\n\n\nb") == "a\n\nb"
    assert llm.clean("  \n\na\n\n\nb  ") == "a\n\nb"
    assert llm.clean("a\n\nb") == "a\n\nb"  # one blank line is kept


def test_reply_sends_system_prompt_and_appends_history(fake_llm, cfg):
    answer = llm.reply("Hi Riko!", cfg)
    assert answer == REPLY

    req = fake_llm.chat_requests[-1]
    assert req["messages"][0] == {"role": "system", "content": cfg["system_prompt"]}
    assert req["messages"][1:] == [{"role": "user", "content": "Hi Riko!"}]
    assert req["model"] == "test-model"  # auto-discovered from /v1/models

    history = json.loads(Path(cfg["history_file"]).read_text())
    assert history == [
        {"role": "user", "content": "Hi Riko!"},
        {"role": "assistant", "content": REPLY},
    ]


def test_reply_streams_from_fake_server(fake_llm, cfg):
    # llm.reply prefers stream:true; the fake server only speaks SSE on that flag
    assert llm.reply("Stream for me", cfg) == REPLY
    assert fake_llm.chat_requests[-1].get("stream") is True


def test_reply_falls_back_to_non_streaming(fake_llm, cfg):
    fake_llm.stream_ok = False  # server 400s on stream:true, like a completion-only server
    assert llm.reply("hi", cfg) == REPLY
    assert fake_llm.chat_requests[-1].get("stream") is not True


def test_reply_strips_think_from_model_output(fake_llm, cfg):
    fake_llm.reply = f"{OPEN}hidden reasoning here{CLOSE}\n\nThe answer is two, senpai."
    assert llm.reply("2+2?", cfg) == "The answer is two, senpai."


def test_history_is_trimmed_to_max_history(fake_llm, cfg, monkeypatch):
    # 10 pre-existing turns, max_history 4 -> system + last 4 messages go out
    monkeypatch.setitem(cfg["llm"], "max_history", 4)
    Path(cfg["history_file"]).write_text(json.dumps(
        [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]))
    llm.reply("latest", cfg)
    sent = fake_llm.chat_requests[-1]["messages"]
    assert sent[0]["role"] == "system"
    # the fresh user message counts inside the window: last 4 = m7, m8, m9, latest
    assert [m["content"] for m in sent[1:]] == ["m7", "m8", "m9", "latest"]
    # the file keeps the whole conversation; trimming applies to what is sent
    history = json.loads(Path(cfg["history_file"]).read_text())
    assert len(history) == 12  # 10 old + user + assistant
    assert history[-2] == {"role": "user", "content": "latest"}


def test_none_max_history_falls_back_to_default(fake_llm, cfg, monkeypatch):
    monkeypatch.setitem(cfg["llm"], "max_history", None)  # `max_history:` left blank in the yaml
    assert llm.reply("hi", cfg) == REPLY


def test_damaged_history_file_is_replaced_not_fatal(fake_llm, cfg):
    Path(cfg["history_file"]).write_text("[{not json")
    assert llm.reply("hi again", cfg) == REPLY
    history = json.loads(Path(cfg["history_file"]).read_text())
    assert history == [
        {"role": "user", "content": "hi again"},
        {"role": "assistant", "content": REPLY},
    ]


def test_empty_model_answer_gets_placeholder(fake_llm, cfg):
    fake_llm.reply = "   "
    assert llm.reply("hi", cfg).startswith("...sorry senpai")


# ── reasoning models that spend the whole budget thinking ──────────────────


def test_length_truncation_retries_with_double_max_tokens(fake_llm, cfg):
    # first call (1024 tokens) gets only hidden thinking + finish_reason='length';
    # the retry at 2048 is under the fake server's truncation limit, so it answers.
    fake_llm.truncate_below = 2048
    assert llm.reply("hi Riko, one short sentence please", cfg) == REPLY
    budgets = [r["max_tokens"] for r in fake_llm.chat_requests]
    assert budgets == [1024, 2048]  # exactly one retry, doubled
    history = json.loads(Path(cfg["history_file"]).read_text())
    assert history[-1] == {"role": "assistant", "content": REPLY}


def test_retry_also_empty_falls_back_and_stops(fake_llm, cfg):
    # every budget gets truncated -> one retry at double tokens, then the fallback line
    fake_llm.truncate_below = 10 ** 9
    answer = llm.reply("hi", cfg)
    assert answer.startswith("...sorry senpai")
    assert [r["max_tokens"] for r in fake_llm.chat_requests] == [1024, 2048]
    history = json.loads(Path(cfg["history_file"]).read_text())
    assert history[-1] == {"role": "assistant", "content": answer}


def test_retry_capped_at_16384(fake_llm, cfg, monkeypatch):
    monkeypatch.setitem(cfg["llm"], "max_tokens", 16384)  # doubling would exceed the cap
    fake_llm.truncate_below = 10 ** 9
    assert llm.reply("hi", cfg).startswith("...sorry senpai")
    assert [r["max_tokens"] for r in fake_llm.chat_requests] == [16384]  # no point retrying


def test_default_max_tokens_is_4096(fake_llm, cfg, monkeypatch):
    monkeypatch.delitem(cfg["llm"], "max_tokens")  # key left out of the yaml
    assert llm.reply("hi", cfg) == REPLY
    assert fake_llm.chat_requests[-1]["max_tokens"] == 4096


def test_reasoning_effort_and_extra_body_reach_the_server(fake_llm, cfg, monkeypatch):
    monkeypatch.setitem(cfg["llm"], "reasoning_effort", "low")
    monkeypatch.setitem(cfg["llm"], "extra_body",
                        {"chat_template_kwargs": {"enable_thinking": False}})
    assert llm.reply("hi", cfg) == REPLY
    req = fake_llm.chat_requests[-1]
    assert req["reasoning_effort"] == "low"
    assert req["chat_template_kwargs"] == {"enable_thinking": False}  # extra_body is merged in


def test_passthrough_survives_non_streaming_fallback(fake_llm, cfg, monkeypatch):
    fake_llm.stream_ok = False  # the plain path must forward the kwargs too
    monkeypatch.setitem(cfg["llm"], "reasoning_effort", "minimal")
    monkeypatch.setitem(cfg["llm"], "extra_body", {"top_k": 5})
    assert llm.reply("hi", cfg) == REPLY
    req = fake_llm.chat_requests[-1]
    assert req.get("stream") is not True
    assert req["reasoning_effort"] == "minimal"
    assert req["top_k"] == 5


def test_no_passthrough_keys_omitted_from_body(fake_llm, cfg):
    assert llm.reply("hi", cfg) == REPLY
    assert "reasoning_effort" not in fake_llm.chat_requests[-1]
    assert "chat_template_kwargs" not in fake_llm.chat_requests[-1]


def test_forget_wipes_history(fake_llm, cfg):
    llm.reply("hi", cfg)
    assert Path(cfg["history_file"]).read_text().strip()
    llm.forget(cfg)
    assert not Path(cfg["history_file"]).exists()
