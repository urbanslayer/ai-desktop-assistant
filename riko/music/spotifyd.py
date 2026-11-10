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

Ducking: while she speaks, the player's MPRIS Volume drops via the session
bus and comes back when she stops. Purely best-effort — no dbus, no
MPRIS player, or a grumpy bus means a silent no-op (detect, never install).

Test seams: `which`/`popen` on SpotifydPlayer, `SESSION_BUS` (module-level
factory) on MprisDuck — no compositor, no live session bus needed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

DEFAULT_DEVICE_NAME = "Riko"
DEFAULT_BACKEND = "pipewire"          # this desktop's audio server
DEFAULT_DUCK_FRACTION = 0.15          # player volume while she speaks
MPRIS_PLAYERS = ("spotifyd", "spotify")   # duck-worthy names, in order
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# The exact owner-facing setup text for a missing binary (never install).
SETUP_TEXT = (
    "spotifyd is not installed, so Riko can't start the desktop player "
    "(Riko never installs anything). One-time setup: install it "
    "(Arch: sudo pacman -S spotifyd, otherwise: cargo install --force "
    "--locked spotifyd), then authenticate once in a terminal: run "
    "'spotifyd authenticate' and open the login URL it prints. After that "
    "the tray's Music > Start player works.")


class SpotifydPlayer:
    """The tray's handle on the spotifyd child process. `mode` comes from
    desktop.music.spotifyd (off|managed|external); only `managed` spawns.
    A binary that exists is REQUIRED before any spawn — we run what's
    there, we never fetch what isn't."""

    def __init__(self, music_cfg: dict | None, device_name: str = DEFAULT_DEVICE_NAME,
                 *, which=shutil.which, popen=subprocess.Popen):
        cfg = dict(music_cfg or {})
        mode = str(cfg.get("spotifyd") or "off").strip().lower()
        self.mode = mode if mode in ("off", "managed", "external") else "off"
        self.device_name = (str(device_name or "").strip() or DEFAULT_DEVICE_NAME)
        self.backend = (str(cfg.get("backend") or "").strip() or DEFAULT_BACKEND)
        self.binary_override = str(cfg.get("binary") or "").strip()
        self.extra_args = [str(a) for a in (cfg.get("extra_args") or [])]
        self._which = which
        self._popen = popen
        self._proc = None

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

    # -- lifecycle ---------------------------------------------------------
    def argv(self) -> list[str]:
        return ([self.find_binary() or "spotifyd",
                 "--device-name", self.device_name,
                 "--backend", self.backend,
                 "--no-daemon"] + self.extra_args)

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
        try:
            self._proc = self._popen(self.argv(),
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        except OSError as e:
            self._proc = None
            return {"ok": False,
                    "message": f"spotifyd would not start: {e} — is it "
                               f"runnable? One-time login may be missing: "
                               f"run 'spotifyd authenticate' in a terminal"}
        time.sleep(0.3)               # a binary that dies instantly is a fail
        if not self.running():
            code = self._proc.returncode if self._proc is not None else "?"
            self._proc = None
            return {"ok": False,
                    "message": f"spotifyd exited right away (code {code}) — "
                               f"the one-time login may be missing: run "
                               f"'spotifyd authenticate' in a terminal"}
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
            return self._bus or {}
        factory = SESSION_BUS or _dbus_props_bus
        try:
            return factory() or {}
        except Exception:
            return {}

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
