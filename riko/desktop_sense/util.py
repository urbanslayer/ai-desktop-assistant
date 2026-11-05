"""Small shared helpers: subprocess running, JSON parsing, title hygiene."""
import json
import subprocess

TITLE_MAX = 120            # docs/DESKTOP_SENSE.md: titles capped at 120 chars


def clean_title(title):
    """Collapse whitespace and cap at the contract's 120 chars."""
    t = " ".join(str(title or "").split())
    return t[:TITLE_MAX]


def run(argv, timeout=10, runner=None, **kw):
    """Run argv (a list — never a shell string) and return stdout as text.

    `runner` (tests / fakes) replaces subprocess.run wholesale and must
    return an object with .returncode/.stdout/.stderr.
    """
    if runner is not None:
        return runner(list(argv), timeout=timeout, **kw)
    return subprocess.run(list(argv), capture_output=True, timeout=timeout,
                          text=True, **kw)


def run_json(argv, timeout=10, runner=None, error="command failed"):
    """Run argv and parse stdout as JSON; ToolError on nonzero rc or junk."""
    from .api_errors import ToolError  # function-level: keeps fake-runner tests cheap
    try:
        done = run(argv, timeout=timeout, runner=runner)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise ToolError(f"{argv[0]} timed out") from None
    if done.returncode != 0:
        raise ToolError(f"{error}: {(done.stderr or done.stdout or '').strip()[:200]}")
    try:
        return json.loads(done.stdout or "")
    except ValueError:
        raise ToolError(f"{error}: {argv[0]} printed something we could not parse") from None


def parse_position(value):
    """'2560x0' -> (2560, 0); anything unparsable -> (0, 0)."""
    try:
        x, _, y = str(value).partition("x")
        return (int(float(x)), int(float(y)))
    except (ValueError, TypeError):
        return (0, 0)
