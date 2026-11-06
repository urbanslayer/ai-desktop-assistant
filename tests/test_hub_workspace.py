"""Workspace pre-act + refusal guard: the hub performs explicit
workspace-switch requests itself (through the client-tool channel, before
the LLM) and refuses to let a tool-backed request end in a shrug.

hub/intents.py decides; hub/server.py acts. Fake LLM + fake desktop client
(real long-poll channel via LiveHub/FakeDesktop from test_hub_clients). See
docs/API.md "Workspace pre-act" / "Claimed-action guard" (refusal twin).
"""
import json

import pytest
import requests

from hub import intents
from test_hub_clients import FakeDesktop, LiveHub, api
from test_hub_tools import ScriptedLLM, call_step

# ── the workspace matcher table (the contract) ──────────────────────────────

WS_MATCH = [
    ("Switch to Workspace 4.", "4", None),          # the live bug phrasing
    ("switch to workspace 4", "4", None),
    ("Go to desktop 2", "2", None),
    ("change to virtual desktop 3", "3", None),
    ("move to ws 7", "7", None),
    ("switch to #5", "5", None),
    ("switch to workspace gaming", "gaming", None),
    ("switch to the coding desktop", "coding", None),
    ("please switch to workspace 2", "2", None),
    ("could you switch to desktop 5", "5", None),
    ("just go to desktop 1", "1", None),
    ("switch to workspace 4 on the left screen", "4", "left"),
    ("go to desktop 2 on the portable screen", "2", "portable"),
    ("switch to workspace 6 to the left Dell screen", "6", "left Dell"),
    ("jump to workspace 2", "2", None),
    ("try to switch to workspace 4", "4", None),
]

WS_NO_MATCH = [
    "what workspace am I on?",
    "did you switch to workspace 4?",
    "how do I switch workspaces?",
    "I switched to workspace 3 earlier",
    "am I on workspace 4?",
    "switch to the other desktop",        # vague target
    "switch to workspace",                # no target
    "open firefox",
    "hello",
    "what's the weather",
    "I love this desktop wallpaper",
    "turn on the lights",
]

WS_BARE = ["switch to 4", "go to 3", "move to 7", "switch over to 2"]


@pytest.mark.parametrize("text,target,monitor", WS_MATCH)
def test_workspace_switch_requests_match(text, target, monitor):
    got = intents.detect_workspace_switch_request(text)
    assert got is not None, "should match"
    assert got["workspace"] == target
    assert got["monitor"] == monitor


@pytest.mark.parametrize("text", WS_NO_MATCH)
def test_non_switch_messages_never_match(text):
    assert intents.detect_workspace_switch_request(text) is None


@pytest.mark.parametrize("text", WS_BARE)
def test_bare_switch_needs_a_prior_workspace_turn(text):
    assert intents.detect_workspace_switch_request(text) is None
    got = intents.detect_workspace_switch_request(text, prev_switch=True)
    assert got is not None and got["workspace"].isdigit()


# ── the refusal matcher table ───────────────────────────────────────────────

REFUSAL_HITS = [
    # the live reply, nearly verbatim
    ("the tool reports the switch, yet your screen clearly isn't following "
     "it, and I have no way to actually press Super+4"),
    "I can't switch workspaces for you, senpai.",
    "I'm unable to press anything on your keyboard.",
    "there's no way to move windows from here",
    "you'll have to press Super+4 yourself, senpai!",
    "I cannot open apps on your machine.",
    "just press the Super key, then 4",
]

REFUSAL_MISSES = [
    "Done — you're on workspace 4 now.",
    "On workspace 4, senpai!",
    "Which workspace did you mean?",
    "The weather is lovely today.",
    "I can switch whenever you like.",
    "Workspace 4 has your terminal open.",
]


@pytest.mark.parametrize("reply", REFUSAL_HITS)
def test_refusal_matches(reply):
    assert intents.refuses_tool_backed_action(reply)


@pytest.mark.parametrize("reply", REFUSAL_MISSES)
def test_refusal_spares(reply):
    assert not intents.refuses_tool_backed_action(reply)


KIND_CASES = [
    ("switch to workspace 4", "workspace"),
    ("switch to 4", "workspace"),
    ("open firefox", "open"),
    ("focus the terminal", "focus"),
    ("bring up the editor", "focus"),
    ("switch to the previous window", "window"),
    ("turn on the lights", "device"),
    ("set a timer for 10 minutes", "timer"),
    ("remind me to stretch", "timer"),
    ("hello", None),
    ("what workspace am I on?", None),
]


@pytest.mark.parametrize("text,kind", KIND_CASES)
def test_detect_action_request_kind(text, kind):
    assert intents.detect_action_request_kind(text) == kind


# ── the pre-act through /api/chat (fake LLM + fake desktop client) ──────────

SWITCH_TOOL = {"name": "switch_workspace", "risk": "act",
               "permission_default": "always",
               "description": "Show a workspace / virtual desktop",
               "parameters": {"type": "object", "properties": {
                   "workspace": {"type": "string"},
                   "monitor": {"type": "string"}}}}
OPEN_APP_TOOL = {"name": "open_app", "risk": "act",
                 "permission_default": "always",
                 "description": "Launch an installed app",
                 "parameters": {"type": "object", "properties": {
                     "app": {"type": "string"}}}}


@pytest.fixture
def hub_ws(tmp_path):
    """build(steps, tools=..., answer=..., tools_defaults=...) -> (hub, llm, desk)."""
    made = []

    def build(steps, tools=None, answer=None, tools_defaults=None):
        llm = ScriptedLLM(steps)
        made.append(llm)
        hub = LiveHub(llm, tmp_path, tools_defaults)
        made.append(hub)

        def default_answer(req):
            return {"ok": True,
                    "result": {"workspace": req["arguments"].get("workspace"),
                               "active": True}}

        desk = FakeDesktop(
            hub,
            tools=tools if tools is not None else [SWITCH_TOOL],
            answer=answer or default_answer)
        made.append(desk)
        return hub, llm, desk

    yield build
    for m in reversed(made):
        if isinstance(m, FakeDesktop):
            m.close()
        else:
            m.stop()


def chat(hub, text):
    return requests.post(f"{api(hub)}/api/chat", json={"text": text},
                         timeout=30).json()


FALSE_DONE = "Done — you're on workspace 4 now."


def test_preact_runs_the_switch_and_injects_the_result(hub_ws):
    hub, llm, desk = hub_ws([{"content": FALSE_DONE}])
    res = chat(hub, "Switch to Workspace 4.")
    # the hub called the client tool itself, no approval gate
    assert len(desk.seen) == 1
    req = desk.seen[0]
    assert req["tool"] == "switch_workspace"
    assert req["arguments"] == {"workspace": "4"}
    assert req["needs_approval"] is False
    # the result + instruction were already in the ONE model call
    assert len(llm.chat_requests) == 1
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert ("You just switched the owner's screen to workspace 4 via the "
            "desktop tool" in sysmsg)
    assert '"workspace": "4"' in sysmsg
    assert ("Do not call switch_workspace again for this request" in sysmsg)
    # a completion claim on a pre-acted turn does NOT trigger a guard retry
    assert res["reply"] == FALSE_DONE
    # chip + stored trace + tools log + events
    assert res["tools_used"] == [
        {"tool": "desktop__switch_workspace",
         "label": "switching to workspace 4", "source": "client",
         "auto": True}]
    log = requests.get(f"{api(hub)}/api/tools/log", timeout=10).json()["entries"]
    assert log[0]["tool"] == "desktop__switch_workspace"
    assert log[0]["ok"] is True and log[0]["source"] == "client"
    assert log[0]["approval"] == "auto (hub pre-act)"
    evs = requests.get(f"{api(hub)}/api/events?after=0&wait=0",
                       timeout=10).json()["events"]
    assert any(e["type"] == "tool_started"
               and e["tool"] == "desktop__switch_workspace"
               and e["label"] == "switching to workspace 4" for e in evs)


def test_preact_error_path_injects_the_failure(hub_ws):
    hub, llm, desk = hub_ws(
        [{"content": "The switch failed — workspace 9 does not exist."}],
        answer=lambda req: {"ok": False,
                            "error": "workspace 9 does not exist"})
    res = chat(hub, "switch to workspace 9")
    assert len(llm.chat_requests) == 1
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert ("it failed: workspace 9 does not exist" in sysmsg)
    assert "You just switched" not in sysmsg
    assert res["tools_used"] == [
        {"tool": "desktop__switch_workspace",
         "label": "switching to workspace 9", "source": "client",
         "auto": True}]
    log = requests.get(f"{api(hub)}/api/tools/log", timeout=10).json()["entries"]
    assert log[0]["ok"] is False and "workspace 9" in log[0]["error"]


def test_preact_passes_the_screen_phrase(hub_ws):
    hub, _, desk = hub_ws([{"content": "On 4 over there, senpai."}])
    res = chat(hub, "switch to workspace 4 on the left screen")
    assert desk.seen[0]["arguments"] == {"workspace": "4", "monitor": "left"}
    assert res["tools_used"][0]["label"] == \
        "switching to workspace 4 on the left screen"


def test_no_preact_when_the_tool_is_not_offered(hub_ws):
    hub, llm, desk = hub_ws([{"content": "Which workspace, senpai?"}],
                            tools=[{"name": "open_url", "risk": "read",
                                    "description": "Open a URL"}])
    res = chat(hub, "Switch to Workspace 4.")
    assert desk.seen == []   # no switch_workspace dispatch
    assert "You just switched the owner's screen" not in \
        json.dumps(llm.chat_requests[0]["messages"])
    assert res["tools_used"] == []


def test_permission_never_skips_preact(hub_ws):
    hub, llm, desk = hub_ws(
        [{"content": "Which workspace, senpai?"}],
        tools_defaults={"permissions": {"desktop__switch_workspace": "never"}})
    res = chat(hub, "Switch to Workspace 4.")
    assert desk.seen == []
    assert "You just switched the owner's screen" not in \
        json.dumps(llm.chat_requests[0]["messages"])
    assert res["tools_used"] == []


def test_bare_switch_pre_acts_only_after_a_workspace_turn(hub_ws):
    hub, llm, desk = hub_ws([{"content": "On 4, senpai."},
                             {"content": "On 3 now."}])
    chat(hub, "switch to workspace 4")          # named: pre-act
    chat(hub, "switch to 3")                    # bare, AFTER a switch turn
    assert [r["arguments"] for r in desk.seen] == [{"workspace": "4"},
                                                   {"workspace": "3"}]
    assert len(llm.chat_requests) == 2


def test_bare_switch_without_a_workspace_turn_does_not_pre_act(hub_ws):
    hub, llm, desk = hub_ws([{"content": "Hi there, senpai!"},
                             {"content": "Which number, senpai?"}])
    chat(hub, "hello")                          # assistant turn, no tools
    res = chat(hub, "switch to 4")              # bare: NO context yet
    assert desk.seen == []
    assert "You just switched the owner's screen" not in \
        json.dumps(llm.chat_requests[1]["messages"])
    assert res["tools_used"] == []


# ── the refusal guard through /api/chat ─────────────────────────────────────

REFUSE = "I can't press Super+4, senpai — I have no way to do that myself."


def test_refusal_reruns_once_with_the_refusal_nudge(hub_ws):
    hub, llm, desk = hub_ws([{"content": REFUSE},
                             {"content": "Opening Firefox now."}],
                            tools=[OPEN_APP_TOOL])
    res = chat(hub, "open firefox")
    assert res["reply"] == "Opening Firefox now."
    assert len(llm.chat_requests) == 2
    nudge = llm.chat_requests[1]["messages"][-1]
    assert nudge["role"] == "system"
    assert nudge["content"] == \
        "You have a tool for this; call it instead of refusing."
    # the refused reply is discarded: never stored
    hist = requests.get(f"{api(hub)}/api/history", timeout=10).json()["messages"]
    assert [m["content"] for m in hist if m["role"] == "assistant"] == \
        ["Opening Firefox now."]


def test_refusal_not_retried_without_a_matching_tool(hub_ws):
    hub, llm, desk = hub_ws([{"content": REFUSE}],
                            tools=[{"name": "open_url", "risk": "read",
                                    "description": "Open a URL"}])
    res = chat(hub, "open firefox")
    # open_app was NOT offered (get_time/weather can't open apps): stands
    assert res["reply"] == REFUSE
    assert len(llm.chat_requests) == 1


def test_refusal_shares_the_one_retry_cap(hub_ws):
    hub, llm, desk = hub_ws([{"content": REFUSE},
                             {"content": "I'm unable to, senpai."},
                             {"content": "NEVER REACHED"}],
                            tools=[OPEN_APP_TOOL])
    res = chat(hub, "open firefox")
    assert res["reply"] == "I'm unable to, senpai."
    assert len(llm.chat_requests) == 2


def test_refusal_never_fires_on_a_plain_reply(hub_ws):
    hub, llm, desk = hub_ws([{"content": "Firefox is great, senpai!"}],
                            tools=[OPEN_APP_TOOL])
    res = chat(hub, "tell me about firefox")
    assert res["reply"] == "Firefox is great, senpai!"
    assert len(llm.chat_requests) == 1


def test_turn_with_a_tool_call_never_meets_the_refusal_nudge(hub_ws):
    hub, llm, desk = hub_ws([
        call_step("desktop__open_app", {"app": "firefox"}),
        {"content": "I couldn't, honestly."}],
        tools=[OPEN_APP_TOOL])
    res = chat(hub, "open firefox")
    # a tool ran: the reply stands, no nudge in any request
    assert res["reply"] == "I couldn't, honestly."
    assert all("call it instead of refusing" not in json.dumps(r["messages"])
               for r in llm.chat_requests)
