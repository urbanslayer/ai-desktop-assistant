"""Tiny stdio MCP server fixture (docs/ROUNDS.md R2-M tests).

Run with the repo venv python: `python tests/fixtures/mcp_echo_server.py`.
Two tools:

  echo(text)      — readOnlyHint, returns the text back
  add_note(text)  — destructiveHint, appends to an in-memory list

Test hooks via env vars (so one file covers the error/crash/timeout cases):
  ECHO_HANG=30     every echo sleeps 30 s first (timeout tests)
  ECHO_FAIL=1      echo raises (tool call comes back isError)
  ECHO_CRASH=1     the first echo answers, then the process exits (crash ->
                   reconnect tests)
"""
import os
import sys
import threading
import time

try:
    from mcp.server.mcpserver import MCPServer          # mcp >= 2
except ImportError:  # mcp < 2 kept the FastMCP name
    from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("echo")
NOTES: list[str] = []
_crashes = [0]


@server.tool(description="Echo the text straight back.",
             annotations=ToolAnnotations(read_only_hint=True,
                                         open_world_hint=False))
def echo(text: str) -> str:
    if os.environ.get("ECHO_HANG"):
        time.sleep(float(os.environ["ECHO_HANG"]))
    if os.environ.get("ECHO_FAIL"):
        raise ValueError("echo is broken on purpose")
    if os.environ.get("ECHO_CRASH") and _crashes[0] == 0:
        _crashes[0] += 1
        # answer first, then die like a crashed server
        threading.Timer(0.3, lambda: os._exit(7)).start()
    return f"echo: {text}"


@server.tool(description="Append a note (changes state!).",
             annotations=ToolAnnotations(destructive_hint=True))
def add_note(text: str) -> str:
    NOTES.append(text)
    return f"noted ({len(NOTES)}): {text}"


if __name__ == "__main__":
    # keep stdout clean for the protocol; debug output would break framing
    print("echo-fixture up", file=sys.stderr, flush=True)
    if "--http" in sys.argv:  # tests/fixtures http-transport coverage
        port = int(sys.argv[sys.argv.index("--http") + 1])
        server.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        server.run()
