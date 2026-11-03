"""Hub-side built-in tools (docs/ROUNDS.md R1-B, docs/TOOLS.md A).

`web_search` (SearXNG JSON), `fetch_page` (readable text, SSRF-guarded) and
`get_time`. The tool loop in toolloop.py only ever talks to ToolSpec objects,
so client tools (clients.py) and — later, round 2 — MCP tools slot in the
same way.
"""
from .registry import (TOOL_NAME_RE, ToolSpec, build_hub_specs, full_tool_name,
                       hub_tool_info, openai_tools, permission_for)

__all__ = ["ToolSpec", "build_hub_specs", "openai_tools", "permission_for",
           "full_tool_name", "hub_tool_info", "TOOL_NAME_RE"]
