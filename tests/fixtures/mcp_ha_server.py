"""A tiny fake Home Assistant 'Model Context Protocol Server' for tests.

Mirrors the official integration's /api/mcp/assist surface as probed on the
owner's HA (protocol 2025-03-26, serverInfo home-assistant): domain-prefixed
tool names (intent__HassTurnOn, light__HassLightSet, homeassistant__GetLiveContext,
…), one no-argument prompt 'Assist' (long enough to cross the hub's per-server
cap), and streamable HTTP served at exactly /api/mcp/assist.

    python tests/fixtures/mcp_ha_server.py --http <port>

The 'whoami' tool echoes back the Authorization header it received so tests
can prove the hub expanded a ${NAME} placeholder before connecting.
"""
import argparse
import socketserver
import sys
from http.server import BaseHTTPRequestHandler

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer, Context
except ImportError:  # older layouts
    from mcp.server.fastmcp import FastMCP as MCPServer
    Context = None

ASSIST_INSTRUCTIONS = (
    "Smart Home MCP: you control the owner's Home Assistant with the tools "
    "here. Prefer a tool call over guessing. Speak device names, never "
    "entity ids. "
    + "Filler keeps this prompt long so the hub must truncate it. " * 40
    + "END-OF-INSTRUCTIONS"
)


def build_server() -> MCPServer:
    server = MCPServer("homeassistant")

    @server.prompt(name="Assist")
    def assist() -> str:
        return ASSIST_INSTRUCTIONS

    def _auth(ctx) -> str:
        try:
            return str(ctx.request_context.request.headers.get("authorization"))
        except Exception:
            return "<none>"

    @server.tool()
    async def whoami(ctx: Context) -> str:
        return f"auth: {_auth(ctx)}"

    @server.tool(name="intent__HassTurnOn")
    async def turn_on(name: str = "", area: str = "") -> str:
        return f"ok: on {name or area}"

    @server.tool(name="intent__HassTurnOff")
    async def turn_off(name: str = "", area: str = "") -> str:
        return f"ok: off {name or area}"

    @server.tool(name="light__HassLightSet")
    async def light_set(name: str = "", brightness: int = 0) -> str:
        return "ok: light set"

    @server.tool(name="climate__HassClimateSetTemperature")
    async def climate_set(name: str = "", temperature: float = 0.0) -> str:
        return "ok: climate set"

    @server.tool(name="intent__HassSetPosition")
    async def set_position(name: str = "", position: int = 0) -> str:
        return "ok: position set"

    @server.tool(name="intent__HassStopMoving")
    async def stop_moving(name: str = "", area: str = "") -> str:
        return "ok: stopped"

    @server.tool(name="intent__HassCancelAllTimers")
    async def cancel_timers() -> str:
        return "ok: timers cancelled"

    @server.tool(name="homeassistant__GetLiveContext")
    async def live_context() -> str:
        return "Kitchen light: on. Living room: 21.4 C."

    @server.tool(name="assist_satellite__HassBroadcast")
    async def broadcast(message: str = "", area: str = "") -> str:
        return "ok: broadcast"

    @server.tool(name="llm__GetDateTime")
    async def get_datetime() -> str:
        return "2026-11-04T12:00:00+01:00"

    return server


class _Raw(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Stub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(404)
        self.end_headers()

    log_message = lambda *a: None  # noqa: E731


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int)
    args = ap.parse_args()
    server = build_server()
    if args.http:
        # the real integration serves exactly this path
        server.run(transport="streamable-http", host="127.0.0.1",
                   port=args.http, streamable_http_path="/api/mcp/assist")
        return
    with _Raw(("127.0.0.1", 0), _Stub) as stub:
        port = stub.server_address[1]
    server.run(transport="streamable-http", host="127.0.0.1", port=port,
               streamable_http_path="/api/mcp/assist")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
