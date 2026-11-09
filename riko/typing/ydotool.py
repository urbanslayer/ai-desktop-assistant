"""The ydotool backend (docs/TYPING.md §2): uinput events injected at the
compositor level, so the focused window is the only target.

NEVER run ydotool/ydotoold for real in tests — the conftest guard raises if
a test tries. Availability is checked before anything is sent; a missing
binary, daemon or socket fails with the owner's setup text, never with a
half-typed sentence."""
import os
import shutil
import socket

from ..desktop_sense.util import run as _util_run

OWNER_SETUP = (
    "Keystroke injection is not set up. Riko types with ydotool; the owner\n"
    "installs and enables it (Riko never installs packages or starts daemons):\n\n"
    "    sudo pacman -S ydotool\n"
    "    # uinput access for the daemon (one-time):\n"
    "    echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", "
    "OPTIONS+=\"static_node=uinput\"' | sudo tee "
    "/etc/udev/rules.d/80-uinput.rules\n"
    "    sudo udevadm control --reload && sudo udevadm trigger\n"
    "    sudo usermod -aG input $USER   # log out and in again\n"
    "    systemctl --user enable --now ydotool.service\n\n"
    "Note: membership of `input` lets any program you run read every input\n"
    "device. The alternative is a root ydotoold with its socket chowned to\n"
    "you; Riko honours YDOTOOL_SOCKET either way.\n"
)

DEFAULT_SOCKET = "/run/.ydotool_socket"


class YdotoolUnavailable(RuntimeError):
    """Nothing was sent; OWNER_SETUP explains what the owner must do."""


class YdotoolBackend:
    """Sends `ydotool key --keydelay <ms> CODE:1 CODE:0 …` through an
    injectable runner (tests: fake; production: riko.desktop_sense.util.run,
    which already strips the live-session env for us)."""

    def __init__(self, runner=None, env=None, key_delay_ms=8):
        self.runner = runner or _util_run
        self.env = dict(env if env is not None else os.environ)
        self.key_delay_ms = max(1, int(key_delay_ms))

    # ── availability (§2: detect, fail cleanly, tell the owner) ──
    def socket_path(self):
        return self.env.get("YDOTOOL_SOCKET") or DEFAULT_SOCKET

    def check(self):
        """None when keystrokes can flow, else the exact reason (a line of
        the §2 decision tree)."""
        if shutil.which("ydotool", path=self.env.get("PATH")) is None:
            return "the `ydotool` binary is not installed"
        if not shutil.which("ydotoold", path=self.env.get("PATH")):
            return "the `ydotoold` daemon binary is not installed"
        sock = self.socket_path()
        if sock != DEFAULT_SOCKET:
            # an explicit socket: probe it — a dead socket must not half-type
            if not self._socket_alive(sock):
                return (f"the ydotool daemon socket {sock} is not accepting "
                        f"connections (is ydotoold running with it?)")
        elif not os.path.exists(sock):
            # default location missing: the service may still serve a session
            # socket, but without YDOTOOL_SOCKET configured we cannot know
            return (f"no ydotool socket at {sock} and YDOTOOL_SOCKET is unset "
                    f"(enable ydotoold.service or point Riko at the daemon)")
        return None

    def _socket_alive(self, path):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(path)
            s.close()
            return True
        except OSError:
            return False

    def require(self):
        reason = self.check()
        if reason:
            raise YdotoolUnavailable(f"{OWNER_SETUP}\nWhat Riko found: {reason}.")

    # ── sending ────────────────────────────────────────────────
    def send_events(self, events):
        """events: [(keycode, down:bool), …] in press-then-release order."""
        if not events:
            return
        self.require()
        argv = ["ydotool", "--keydelay", str(self.key_delay_ms), "key"]
        argv += [f"{int(code)}:{1 if down else 0}" for code, down in events]
        done = self.runner(argv, timeout=30, env=self.env)
        if getattr(done, "returncode", 1) != 0:
            err = (getattr(done, "stderr", "") or "").strip()
            raise YdotoolUnavailable(
                f"ydotool failed to inject keys"
                + (f": {err[:200]}" if err else " (no stderr)")
                + "\nIf the daemon runs as root, Riko needs write access to "
                  "its socket — see the setup notes above.")
