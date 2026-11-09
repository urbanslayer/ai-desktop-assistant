"""The ydotool backend (docs/TYPING.md §2): uinput events injected at the
compositor level, so the focused window is the only target.

NEVER run ydotool/ydotoold for real in tests — the conftest guard raises if
a test tries. Availability is checked before anything is sent; a missing
binary, daemon or socket fails with the owner's setup text, never with a
half-typed sentence."""
import errno
import os
import shutil
import socket

from ..desktop_sense.util import run as _util_run

OWNER_SETUP = (
    "Keystroke injection is not set up. Riko types with ydotool; the owner\n"
    "installs and enables it (Riko never installs packages or starts daemons):\n"
    "\n"
    "    sudo pacman -S ydotool\n"
    "    systemctl --user enable --now ydotool.service\n"
    "\n"
    "The Arch package ships a systemd user service that runs ydotoold and\n"
    "creates its socket at $XDG_RUNTIME_DIR/.ydotool_socket; Riko finds it\n"
    "there automatically (a custom path is honoured via YDOTOOL_SOCKET).\n"
    "The udev/input-group steps are only needed if ydotoold cannot open\n"
    "/dev/uinput — normally logind's uaccess ACL already grants it to the\n"
    "active local session:\n"
    "\n"
    "    echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", "
    "OPTIONS+=\"static_node=uinput\"' | sudo tee "
    "/etc/udev/rules.d/80-uinput.rules\n"
    "    sudo udevadm control --reload && sudo udevadm trigger\n"
    "    sudo usermod -aG input $USER   # log out and in again\n"
    "\n"
    "Note: membership of `input` lets any program you run read every input\n"
    "device. The alternative is a root ydotoold with its socket chowned to\n"
    "you; Riko honours YDOTOOL_SOCKET either way.\n"
    "\n"
    "Harmless daemon log lines: 'running xinput against an Xwayland server'\n"
    "and 'unable to find device pointer:ydotoold virtual device' — ydotoold\n"
    "only tries to tune pointer acceleration via xinput; keyboard injection\n"
    "is unaffected.\n"
)

# Socket resolution order (docs/TYPING.md): YDOTOOL_SOCKET override first,
# then the runtime dir where the systemd user service puts the socket
# ($XDG_RUNTIME_DIR, or /run/user/<uid> when it is unset), then ydotool's
# own default /tmp location. Module-level so tests can redirect them.
_RUN_USER_BASE = "/run/user"
_TMP_SOCKET = "/tmp/.ydotool_socket"

# probe_socket() outcomes
ALIVE, MISSING, DENIED, DEAD = "alive", "missing", "denied", "dead"
# internal-only outcome: the socket file exists but answers a DIFFERENT
# socket type (EPROTOTYPE); probe_socket() retries with the other type.
PROTO_MISMATCH = "_proto_mismatch"


def socket_candidates(env):
    """Socket paths to try, in order (see the note above)."""
    override = (env.get("YDOTOOL_SOCKET") or "").strip()
    if override:
        return [override]
    runtime = (env.get("XDG_RUNTIME_DIR") or "").strip()
    if not runtime and hasattr(os, "getuid"):
        runtime = f"{_RUN_USER_BASE}/{os.getuid()}"
    cands = []
    if runtime:
        cands.append(os.path.join(runtime, ".ydotool_socket"))
    if _TMP_SOCKET not in cands:
        cands.append(_TMP_SOCKET)
    return cands


def _connect(kind, path):
    """One AF_UNIX connect probe (1 s timeout) → an outcome constant.

    Connect only: nothing is ever sent, so the daemon sees a probe, not a
    command. EPROTOTYPE means the socket file exists but was made with the
    other type — reported as PROTO_MISMATCH so probe_socket() can retry."""
    try:
        s = socket.socket(socket.AF_UNIX, kind)
    except OSError:
        return DEAD
    try:
        s.settimeout(1.0)
        s.connect(path)
        return ALIVE
    except FileNotFoundError:
        return MISSING
    except PermissionError:
        return DENIED
    except ConnectionRefusedError:          # stale socket: file, no daemon
        return DEAD
    except OSError as exc:                  # not a socket / timeout / …
        if exc.errno == errno.EPROTOTYPE:
            return PROTO_MISMATCH
        return DEAD
    finally:
        s.close()


def probe_socket(path):
    """AF_UNIX connect probe → ALIVE / MISSING / DENIED / DEAD.

    SOCK_DGRAM is tried first because ydotoold 1.0 binds a datagram socket:
    a SOCK_STREAM connect against it fails with EPROTOTYPE ([Errno 91]
    'Protocol wrong type for socket'), which used to report the live daemon
    as dead. ECONNREFUSED on the datagram connect is a stale socket left by
    a stopped ydotoold. EPROTOTYPE on the datagram probe means the socket
    was made with the old stream type (pre-1.0 ydotoold or a hand-rolled
    daemon), so the probe retries with SOCK_STREAM — stream-only listeners
    stay accepted."""
    status = _connect(socket.SOCK_DGRAM, path)
    if status == PROTO_MISMATCH:
        status = _connect(socket.SOCK_STREAM, path)
        if status == PROTO_MISMATCH:        # cannot happen; be conservative
            status = DEAD
    return status


def resolve_socket(env):
    """(live socket path, None) — or (None, the owner-facing reason)."""
    override = (env.get("YDOTOOL_SOCKET") or "").strip()
    tried = []
    for path in socket_candidates(env):
        status = probe_socket(path)
        tried.append((path, status))
        if status == ALIVE:
            return path, None
        if status == DENIED:
            return None, (f"the ydotool socket {path} exists but Riko may "
                          f"not connect to it (permission denied) — if "
                          f"ydotoold runs as root, its socket must be "
                          f"chowned/chmod'ed for you")
    if override:
        if tried and tried[0][1] == MISSING:
            return None, (f"YDOTOOL_SOCKET={override} does not exist "
                          f"(is ydotoold running with that socket?)")
        return None, (f"the ydotool socket {override} is not accepting "
                      f"connections (stale socket from a stopped daemon?)")
    detail = ", ".join(f"{p} [{s}]" for p, s in tried)
    return None, (f"no ydotool daemon socket is answering — tried: {detail} "
                  f"(enable it with 'systemctl --user enable --now "
                  f"ydotool.service' or point Riko at the daemon with "
                  f"YDOTOOL_SOCKET)")


class YdotoolUnavailable(RuntimeError):
    """Nothing was sent; OWNER_SETUP explains what the owner must do."""


# ydotool 1.0.4 has NO global options (`ydotool --help` lists only the
# subcommands); the delay belongs to the `key` subcommand as
# `--key-delay=N`, and event tokens are `<keycode>:<pressed>` (e.g.
# `28:1 28:0`). `usage: key [OPTION]... [KEYCODES]...`-style text goes to
# STDOUT, so an empty stderr proves nothing on a failure.
MAX_EVENTS_PER_CALL = 2000  # keep argv far below ARG_MAX; engine re-checks
                            # focus/abort between its own chunks

# output snippets that really do mean "the daemon socket refused us"
_PERMISSION_HINTS = ("permission", "denied", "access", "eacces",
                     "operation not permitted")


def _failure_detail(done):
    """The first informative lines of a failed ydotool run, stderr and
    STDOUT alike (ydotool 1.0.4 prints usage errors to stdout)."""
    parts = []
    for name in ("stderr", "stdout"):
        text = str(getattr(done, name, "") or "").strip()
        if text:
            parts.append(f"{name}: {'; '.join(text.splitlines()[:3])[:200]}")
    return "; ".join(parts)


class YdotoolBackend:
    """Sends `ydotool key --key-delay=<ms> CODE:1 CODE:0 …` through an
    injectable runner (tests: fake; production: riko.desktop_sense.util.run).

    The socket resolve_socket() found is ALWAYS handed to the ydotool child
    explicitly as YDOTOOL_SOCKET in the env passed to the runner: the child
    may run with a stripped session env, so it must never have to rediscover
    the socket from inherited variables.

    Batches longer than MAX_EVENTS_PER_CALL events are split over several
    `ydotool key` calls (every engine caller chunks far below this anyway;
    the engine contract re-checks abort/focus between its own chunks)."""

    def __init__(self, runner=None, env=None, key_delay_ms=8):
        self.runner = runner or _util_run
        self.env = dict(env if env is not None else os.environ)
        self.key_delay_ms = max(1, int(key_delay_ms))

    # ── availability (§2: detect, fail cleanly, tell the owner) ──
    def socket_path(self):
        """The socket to use right now, or None when none answers."""
        return resolve_socket(self.env)[0]

    def _ready(self):
        """(live socket, None) or (None, the reason). Each is a line of the
        §2 decision tree."""
        if shutil.which("ydotool", path=self.env.get("PATH")) is None:
            return None, "the `ydotool` binary is not installed"
        if not shutil.which("ydotoold", path=self.env.get("PATH")):
            return None, "the `ydotoold` daemon binary is not installed"
        return resolve_socket(self.env)

    def check(self):
        """None when keystrokes can flow, else the exact reason."""
        return self._ready()[1]

    def require(self):
        reason = self.check()
        if reason:
            raise YdotoolUnavailable(f"{OWNER_SETUP}\nWhat Riko found: {reason}.")

    # ── sending ────────────────────────────────────────────────
    def send_events(self, events, hold_ms=None):
        """events: [(keycode, down:bool), …] in press-then-release order.

        argv follows ydotool 1.0.4 exactly: `ydotool key --key-delay=<ms>
        <code>:1 <code>:0 …` — 1.0.4 has no global options, so the old
        `ydotool --keydelay N key …` form made every call die on its usage
        text. hold_ms overrides the delay for one call (the terminal Enter
        tap uses 30 ms so the key is audibly down before it comes up).
        Long batches split into several calls; the engine contract
        re-checks abort/focus around its own (much smaller) chunks."""
        if not events:
            return
        sock, reason = self._ready()
        if reason:
            raise YdotoolUnavailable(f"{OWNER_SETUP}\nWhat Riko found: {reason}.")
        env = dict(self.env)
        if sock:                          # reason is None ⇒ sock is set
            env["YDOTOOL_SOCKET"] = sock  # explicit; never rely on inheritance
        delay = self.key_delay_ms if hold_ms is None else max(1, int(hold_ms))
        head = ["ydotool", "key", f"--key-delay={delay}"]
        for i in range(0, len(events), MAX_EVENTS_PER_CALL):
            argv = head + [f"{int(code)}:{1 if down else 0}"
                           for code, down in
                           events[i:i + MAX_EVENTS_PER_CALL]]
            done = self.runner(argv, timeout=30, env=env)
            if getattr(done, "returncode", 1) != 0:
                code = getattr(done, "returncode", "?")
                detail = _failure_detail(done)
                out = (str(getattr(done, "stderr", "") or "") + " " +
                       str(getattr(done, "stdout", "") or "")).lower()
                raise YdotoolUnavailable(
                    "ydotool failed to inject keys "
                    f"(exit code {code}"
                    + (f": {detail})" if detail else "; no output)")
                    + ("\nThe daemon socket refused Riko — if ydotoold runs "
                       "as root, Riko needs write access to its socket; see "
                       "the setup notes above."
                       if any(h in out for h in _PERMISSION_HINTS) else ""))
