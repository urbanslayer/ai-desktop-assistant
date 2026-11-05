"""ToolError: the real class from riko.client_tools (so the ClientRuntime's
`except ToolError` catches failures from here), with a standalone fallback
for imports outside the app (plain scripts, isolated unit runs)."""
try:  # the normal path inside the desktop app
    from riko.client_tools import ToolError  # noqa: F401
except Exception:  # pragma: no cover - standalone/loopback fallback
    class ToolError(Exception):
        """The tool ran but failed: reported to the hub as {ok: false, error}."""

__all__ = ["ToolError"]
