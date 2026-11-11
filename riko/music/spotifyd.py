"""Desktop Spotify playback: spotifyd management + best-effort ducking.

Music phase 1 (docs/SPOTIFY_NOTES.md): the hub only STEERS Spotify Connect
devices over the Web API; the sound on this PC comes from spotifyd, a
headless Connect daemon. Riko may start/stop it (owner decision: "spotifyd
managed by Riko", device name "Riko") or stay hands-off:

  character_config.yaml desktop.music.spotifyd:
    off       — the feature is entirely off (no tray items, no listener)
    managed   — Riko starts/stops the spotifyd child process
    external  — you run spotifyd yourself; Riko only talks to it

Riko NEVER installs anything: a missing binary answers with the exact setup
text (pacman + the one-time `spotifyd authenticate`), nothing more.

Managed mode is self-contained — it never reads the owner's
~/.config/spotifyd/spotifyd.conf (that file predates Riko and carries a
RELATIVE cache_path whose credentials landed in ~/cache_directory):
  * Riko writes its OWN minimal config to ~/.config/riko/spotifyd.conf
    (0600, no hooks) and passes `--config-path` + `--cache-path <abs>`;
  * the cache defaults to ~/.cache/riko/spotifyd (config override
    desktop.music.spotifyd_cache) and spotifyd runs with cwd = that dir,
    so nothing relative can ever land in the repo or the home dir;
  * a missing <cache>/oauth/credentials.json triggers DISCOVERY of an
    existing login (~/.cache/spotifyd/oauth, then ~/cache_directory/oauth,
    then $XDG_CACHE_HOME/spotifyd/oauth) which is COPIED in (0600, the
    owner's files are never moved or deleted). With no login anywhere
    Riko does not spawn and says the exact command to run.

Output capture: spotifyd's stdout/stderr are piped into a 20-line ring
buffer drained by a daemon thread (the pipe never blocks), so an early
exit reports spotifyd's own last error line instead of guessing; the
"login missing" advice is only added when the output (or a missing
credentials file) actually points at auth.

Backend note: spotifyd (Arch 0.4.2) accepts exactly
rodio|alsa|pulseaudio|pipe|subprocess — `--backend pipewire` is a clap
usage error (exit 2). PipeWire serves the PulseAudio protocol, so
`pulseaudio` is the right backend even on a PipeWire desktop; a leftover
'pipewire' in the config is mapped with a one-line log note.

Ducking: while she speaks, the player's MPRIS Volume drops via the session
bus and comes back when she stops. Purely best-effort — no dbus, no
MPRIS player, or a grumpy bus means a silent no-op (detect, never install).

Test seams: `which`/`popen`/`log` on SpotifydPlayer, `SESSION_BUS`
(module-level factory) on MprisDuck — no compositor, no live session bus.
"""
from __future__ import annotations

import collections
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_DEVICE_NAME = "Riko"
DEFAULT_BACKEND = "pulseaudio"          # PipeWire serves the PulseAudio protocol
BACKEND_ALIASES = {"pipewire": "pulseaudio", "pipewire-pulse": "pulseaudio"}
DEFAULT_DUCK_FRACTION = 0.15            # player volume while she speaks
OUTPUT_RING = 20                        # spotifyd output lines kept for errors
MPRIS_PLAYERS = ("spotifyd", "spotify")   # duck-worthy names, in order
MPRIS_SELF = "riko"                       # org.mpris.MediaPlayer2.riko = OUR
#     own export (riko/music/mpris.py): never a duck target — Soloist's WS
#     volume ducking already handles her player, and ducking the advertised
#     one would fight the bar's own volume slider.
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# Words that mean "spotifyd itself says the login is the problem".
_AUTH_HINTS = ("credential", "auth", "login", "unauthorized", "token")

# The exact owner-facing setup text for a missing binary (never install).
SETUP_TEXT = (
    "spotifyd is not installed, so Riko can't start the desktop player "
    "(Riko never installs anything). One-time setup: install it "
    "(Arch: sudo pacman -S spotifyd, otherwise: cargo install --force "
    "--locked spotifyd), then authenticate once in a terminal: run "
    "'spotifyd authenticate --cache-path ~/.cache/riko/spotifyd' and open "
    "the login URL it prints. After that the tray's Music > Start player "
    "works.")


def _cache_dir(cfg_value) -> Path:
    """Absolute spotifyd cache dir: config override wins, then
    $RIKO_SPOTIFYD_CACHE (test seam), then ~/.cache/riko/spotifyd."""
    v = str(cfg_value or os.environ.get("RIKO_SPOTIFYD_CACHE") or "").strip()
    if v:
        return Path(v).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base).expanduser() / "riko" / "spotifyd"


def _config_path() -> Path:
    """Where Riko writes ITS OWN spotifyd conf (never the owner's
    ~/.config/spotifyd/spotifyd.conf)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    base = os.environ.get("RIKO_CONFIG_DIR") or str(Path(base) / "riko")
    return Path(base).expanduser() / "spotifyd.conf"


def credential_sources(cache: Path) -> list[Path]:
    """Places an already-authenticated spotifyd may have left its login,
    in priority order. READ-ONLY: we copy, never move or delete. The
    ~/cache_directory entry is the owner's real case — an old conf with
    `cache_path = "cache_directory"` (relative) while authenticate ran
    from ~ dropped the credentials there."""
    home = Path.home()
    out = [home / ".cache" / "spotifyd" / "oauth" / "credentials.json",
           home / "cache_directory" / "oauth" / "credentials.json"]
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        p = Path(xdg).expanduser() / "spotifyd" / "oauth" / "credentials.json"
        if p not in out:
            out.append(p)
    return out


class SpotifydPlayer:
    """The tray's handle on the spotifyd child process. `mode` comes from
    desktop.music.spotifyd (off|managed|external); only `managed` spawns.
    A binary that exists is REQUIRED before any spawn — we run what's
    there, we never fetch what isn't."""

    kind = "spotifyd"

    def __init__(self, music_cfg: dict | None, device_name: str = DEFAULT_DEVICE_NAME,
                 *, which=shutil.which, popen=subprocess.Popen, log=print):
        cfg = dict(music_cfg or {})
        mode = str(cfg.get("spotifyd") or "off").strip().lower()
        self.mode = mode if mode in ("off", "managed", "external") else "off"
        self.device_name = (str(device_name or "").strip() or DEFAULT_DEVICE_NAME)
        raw_backend = (str(cfg.get("backend") or "").strip()
                       or DEFAULT_BACKEND).lower()
        self.backend = BACKEND_ALIASES.get(raw_backend, raw_backend)
        self.backend_note = (
            f"[music] spotifyd backend '{raw_backend}' isn't one spotifyd "
            f"knows (rodio|alsa|pulseaudio|pipe|subprocess); using "
            f"'{self.backend}' — PipeWire serves the PulseAudio protocol"
            if self.backend != raw_backend else "")
        self.binary_override = str(cfg.get("binary") or "").strip()
        self.extra_args = [str(a) for a in (cfg.get("extra_args") or [])]
        self.cache_dir = _cache_dir(cfg.get("spotifyd_cache"))
        self.config_path = _config_path()
        self.log = log
        self._which = which
        self._popen = popen
        self._proc = None
        self._reader = None
        self._output: collections.deque[str] = collections.deque(maxlen=OUTPUT_RING)

    def _note(self, line: str) -> None:
        try:
            self.log(line)
        except Exception:
            pass

    # -- discovery (never installs) --------------------------------------
    def find_binary(self) -> str:
        if self.binary_override:
            return (self.binary_override
                    if os.path.exists(self.binary_override) else "")
        return self._which("spotifyd") or ""

    def setup_needed(self) -> bool:
        return self.mode == "managed" and not self.find_binary()

    def setup_text(self) -> str:
        return SETUP_TEXT

    def credentials_path(self) -> Path:
        return self.cache_dir / "oauth" / "credentials.json"

    def login_missing(self) -> bool:
        return (self.mode == "managed" and bool(self.find_binary())
                and not self.credentials_path().is_file())

    def authenticate_command(self) -> str:
        """The EXACT one-time command for this cache dir."""
        return f"spotifyd authenticate --cache-path {self.cache_dir}"

    # -- managed config (self-contained; ignores the owner's conf) --------
    def conf_text(self) -> str:
        return (
            "# Written by Riko's managed spotifyd start. Edit\n"
            "# character_config.yaml desktop.music instead of this file.\n"
            "# The owner's ~/.config/spotifyd/spotifyd.conf is never read.\n"
            "[global]\n"
            f'device_name = "{self.device_name}"\n'
            f'backend = "{self.backend}"\n'
            f'cache_path = "{self.cache_dir}"\n'
            'device_type = "computer"\n'
            "use_mpris = true\n"
            "bitrate = 320\n"
            "initial_volume = 100\n")

    def _write_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.conf_text()
        try:
            if self.config_path.read_text(encoding="utf-8") == text:
                return                      # already ours; leave the mtime
        except OSError:
            pass
        fd = os.open(str(self.config_path),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(str(self.config_path), 0o600)

    def _ensure_credentials(self):
        """None = login present; str = note about a copy; "" = nowhere found."""
        dest = self.credentials_path()
        try:
            if dest.is_file():
                return None
        except OSError:
            pass
        for src in credential_sources(self.cache_dir):
            try:
                if not src.is_file():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                os.chmod(str(dest), 0o600)
            except OSError:
                continue
            return (f"[music] copied the existing Spotify login from {src} "
                    f"into {dest} (the old file was left where it is)")
        return ""

    # -- lifecycle ---------------------------------------------------------
    def argv(self) -> list[str]:
        return ([self.find_binary() or "spotifyd",
                 "--config-path", str(self.config_path),
                 "--cache-path", str(self.cache_dir),
                 "--device-name", self.device_name,
                 "--backend", self.backend,
                 "--no-daemon"] + self.extra_args)

    def _start_drain(self, proc) -> None:
        """Pipe the child's output into the ring buffer on a daemon thread
        so a chatty spotifyd can never fill the pipe and block."""
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return

        def _drain():
            try:
                for line in stream:
                    line = str(line).strip()
                    if line:
                        self._output.append(line)
            except Exception:
                pass                          # process died mid-read; fine
        self._reader = threading.Thread(target=_drain, daemon=True,
                                        name="spotifyd-output")
        self._reader.start()

    def _last_meaningful(self) -> str:
        lines = [l.strip() for l in self._output if l and l.strip()]
        if not lines:
            return ""
        for line in reversed(lines):          # the actual error beats chatter
            if any(k in line.lower() for k in ("error", "invalid", "failed",
                                               "panic")):
                return line[:300]
        return lines[-1][:300]

    def running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        return proc.poll() is None

    def status(self) -> dict:
        """For the tray line and the (future) client-tool mirror."""
        return {"mode": self.mode,
                "running": self.running(),
                "pid": (self._proc.pid
                        if (self._proc is not None and self._proc.poll() is None)
                        else None),
                "binary": self.find_binary(),
                "device_name": self.device_name,
                "setup_needed": self.setup_needed(),
                "login_needed": self.login_missing(),
                "detail": (SETUP_TEXT if self.setup_needed() else "")}

    def start(self) -> dict:
        if self.mode == "off":
            return {"ok": False,
                    "message": "music is switched off "
                               "(character_config.yaml desktop.music."
                               "spotifyd: off)"}
        if self.mode == "external":
            return {"ok": False,
                    "message": "spotifyd: external — you start and stop it "
                               "yourself; Riko only uses it (tray keeps it "
                               "'managed' to take over)"}
        if self.running():
            return {"ok": True, "message": f"player already running "
                                           f"(pid {self._proc.pid if self._proc else '?'})"}
        binary = self.find_binary()
        if not binary:
            return {"ok": False, "message": SETUP_TEXT}
        if self.backend_note:
            self._note(self.backend_note)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._write_config()
        except OSError as e:
            return {"ok": False,
                    "message": f"can't prepare the spotifyd files "
                               f"({self.config_path} / {self.cache_dir}): {e}"}
        copied = self._ensure_credentials()
        if copied == "":
            return {"ok": False,
                    "message": f"spotifyd has no login yet: run exactly "
                               f"'{self.authenticate_command()}' in a "
                               f"terminal, open the login URL it prints, "
                               f"then Music > Start player again."}
        if copied:
            self._note(copied)
        try:
            self._output.clear()
            proc = self._popen(self.argv(),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               text=True,
                               cwd=str(self.cache_dir))
        except OSError as e:
            self._proc = None
            return {"ok": False,
                    "message": f"spotifyd would not start: {e} — is it "
                               f"runnable? The one-time login may also be "
                               f"missing: run "
                               f"'{self.authenticate_command()}'"}
        self._proc = proc
        self._start_drain(proc)
        time.sleep(0.3)               # a binary that dies instantly is a fail
        if not self.running():
            code = getattr(proc, "returncode", None)
            if self._reader is not None:
                self._reader.join(timeout=0.5)   # let the last lines land
            line = self._last_meaningful()
            self._proc = None
            msg = f"spotifyd exited right away (code {code})"
            if line:
                msg += f" — last output: {line}"
            if any(k in line.lower() for k in _AUTH_HINTS):
                msg += (" — the one-time login may be missing: run exactly "
                        f"'{self.authenticate_command()}' in a terminal")
            else:
                msg += (f" — Riko's own config is {self.config_path} and the "
                        f"cache is {self.cache_dir}")
            return {"ok": False, "message": msg}
        return {"ok": True,
                "message": f"desktop player '{self.device_name}' started "
                           f"(pid {self._proc.pid})"}

    def stop(self) -> dict:
        if self.mode == "off":
            return {"ok": False, "message": "music is switched off"}
        if self.mode == "external":
            return {"ok": False, "message": "spotifyd: external — Riko did "
                                            "not start it and won't stop it"}
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._proc = None
            return {"ok": True, "message": "player is not running"}
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except OSError as e:
            return {"ok": False, "message": f"stop failed: {e}"}
        self._proc = None
        return {"ok": True, "message": "desktop player stopped"}


# ── best-effort MPRIS ducking over D-Bus ────────────────────────────────────

def _dbus_props_bus():
    """Real seam: dbus-python's session bus mapped to our narrow protocol
    {player_name: props-object} (props: Get(iface, prop)/Set(iface, prop,
    value)). Missing dbus or missing players simply come back empty — we
    detect, we never install."""
    try:
        import dbus                                   # type: ignore
    except Exception:
        return {}
    try:
        session = dbus.SessionBus()
    except Exception:
        return {}
    out = {}
    for name in MPRIS_PLAYERS:
        try:
            obj = session.get_object(f"org.mpris.MediaPlayer2.{name}", MPRIS_PATH)
            out[name] = dbus.Interface(
                obj, "org.freedesktop.DBus.Properties")
        except Exception:
            continue
    return out


# test seam (see module docstring); None = build the real dbus-python bus
SESSION_BUS = None


class MprisDuck:
    """Lower the player's MPRIS volume while she speaks, restore after.
    Every path is silent-no-op safe: no bus, no player, no saved state —
    duck()/restore()/on_speak() NEVER raise."""

    def __init__(self, fraction: float = DEFAULT_DUCK_FRACTION, *, bus=None):
        try:
            self.fraction = min(1.0, max(0.0, float(fraction)))
        except (TypeError, ValueError):
            self.fraction = DEFAULT_DUCK_FRACTION
        self._bus = bus
        self._saved: dict[str, float] = {}

    def _players(self) -> dict:
        if self._bus is not None:
            players = self._bus or {}
        else:
            factory = SESSION_BUS or _dbus_props_bus
            try:
                players = factory() or {}
            except Exception:
                return {}
        # Riko's own MPRIS export is never a duck target (see MPRIS_SELF)
        return {n: p for n, p in players.items() if n != MPRIS_SELF}

    def duck(self) -> None:
        if self._saved:                 # already ducked
            return
        for name, props in self._players().items():
            try:
                vol = float(props.Get(MPRIS_PLAYER_IFACE, "Volume"))
                props.Set(MPRIS_PLAYER_IFACE, "Volume",
                          max(0.0, min(1.0, vol * self.fraction)))
                self._saved[name] = vol
            except Exception:
                continue                # this player isn't duckable; next

    def restore(self) -> None:
        players = self._players()
        for name, vol in list(self._saved.items()):
            try:
                players[name].Set(MPRIS_PLAYER_IFACE, "Volume",
                                  max(0.0, min(1.0, vol)))
            except Exception:
                pass
        self._saved.clear()

    def on_speak(self, speaking: bool) -> None:
        """Session.on_speak hook: True = she started, False = she stopped."""
        (self.duck if speaking else self.restore)()
