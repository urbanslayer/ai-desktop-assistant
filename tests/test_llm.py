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


def test_forget_wipes_history(fake_llm, cfg):
    llm.reply("hi", cfg)
    assert Path(cfg["history_file"]).read_text().strip()
    llm.forget(cfg)
    assert not Path(cfg["history_file"]).exists()
