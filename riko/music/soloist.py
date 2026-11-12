"""Desktop Spotify playback via Spotify Soloist (docs/SOLOIST.md).

Soloist is Spotify's OFFICIAL headless Connect player, replacing librespot
based spotifyd after the 2026-11-11 audio-key outage (docs/SPOTIFY_NOTES.md).
Riko manages it end to end: tray Start/Stop, auto-update from Spotify's own
CDN before builds expire (builds die 90 days after their build date, exit
code 10), and WebSocket-API ducking while she speaks. No third-party helper,
no code from soloistd.

Player selection (character_config.yaml desktop.music.player):
    soloist   — the default; Riko starts/stops the child process
    spotifyd  — the legacy librespot path (riko/music/spotifyd.py)
    external  — you run the player yourself; Riko only uses it
    off       — the feature is entirely off
Legacy desktop.music.spotifyd: managed|external|off keeps working when
`player` is absent (see select_player()).

Files (owner-owned, never in the repo):
  key    ~/.config/riko/soloist.key   (0600, written BY THE OWNER; Riko reads
         it and NEVER logs/echoes/sends its value — redact() scrubs it from
         any captured stderr before it surfaces)
  binary ~/.local/share/riko/soloist/bin/soloist
  meta   ~/.local/share/riko/soloist/release.json
         {etag, last_modified, version, build_date, installed_at, sha256}
  data   ~/.local/share/riko/soloist/data   (session; ws.addr/ws.port here)
  cache  ~/.cache/riko/soloist

Auth model (official docs): the API key is passed on EVERY start (-k); the
Spotify Connect session is created once by picking "Riko" in a Spotify app
and lives in the data dir. No browser login, no username/password.

Auto-update: HEAD https://soloist-builds.spotifycdn.com/<archive> and
compare etag/last-modified with release.json; on a difference download
(cap 100 MB), extract ONLY the `soloist` member (absolute paths, '..',
symlinks and oversize entries rejected), chmod 755, verify
`<new> --version` parses and is not already expired, then rename over the
old binary and write release.json. A running player restarts only while
idle — unless the running build expires within 3 days. Failed checks never
interrupt playback and log once per failure kind. Exit code 10 at any time
forces one update + restart cycle.

Test seams: `which`/`popen`/`log`/`ws` on SoloistPlayer; `http`/`runner`/
`log`/`clock` on SoloistUpdater; `connect` on SoloistWs. Tests use fake
HOME, fake popen/http and a fake `soloist` shell script — NEVER the real
CDN and NEVER the owner's daemon. `SCHEDULER` (module level) replaces the
background-thread starter for the periodic check.
"""
from __future__ import annotations

import collections
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DEVICE_NAME = "Riko"
DEFAULT_DUCK_FRACTION = 0.15
DEFAULT_CACHE_SIZE_MB = 500
OUTPUT_RING = 20                     # soloist output lines kept for errors

CDN_HOST = "soloist-builds.spotifycdn.com"
ARCHIVE_URLS = {
    "x86_64": f"https://{CDN_HOST}/soloist_release_x86_64.tar.gz",
    "amd64": f"https://{CDN_HOST}/soloist_release_x86_64.tar.gz",
    "aarch64": f"https://{CDN_HOST}/soloist_release_arm64.tar.gz",
    "arm64": f"https://{CDN_HOST}/soloist_release_arm64.tar.gz",
    "armv7l": f"https://{CDN_HOST}/soloist_release_arm32.tar.gz",
}
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024      # hard cap on a downloaded archive
BUILD_TTL_DAYS = 90                          # builds die 90 days after build
EXPIRE_WARN_DAYS = 7                         # tray warns below this
RESTART_SOON_DAYS = 3                        # ignore "playing" below this
CHECK_INTERVAL_S = 6 * 3600                  # the desktop's periodic check
VERSION_TIMEOUT_S = 10                       # `<new> --version` patience

EXPIRED_EXIT = 10                            # soloist: build expired
_DATA_DIR_BUSY = "already using the data directory"
REDACTION = "«soloist-key»"

# `soloist 1.3.10.61 build 1794376870 (20261111) (g19412df6ea) (linux/x86_64)`
_VERSION_RE = re.compile(r"soloist\s+(\S+)\s+build\s+(\d+)\s+\((\d{8})\)")

KEY_SETUP_TEXT = (
    "Soloist's API key is missing, so Riko won't start the player. In "
    "https://developer.spotify.com/dashboard/soloist generate a Soloist API "
    "key (Premium account), then in a terminal run exactly "
    "'install -D -m 600 /dev/stdin ~/.config/riko/soloist.key', paste the "
    "key, press Enter, then Ctrl-D. The key is a secret: never paste it "
    "into chat.")

PAIR_HINT = ("first run: open Spotify on your phone or PC and pick 'Riko' "
             "in the device list once — she'll remember it")

# test seam (see module docstring); None = threading.Thread(...).start()
SCHEDULER = None


def archive_url(arch: str = "") -> str:
    """The official CDN archive for this architecture (https only). An
    unknown arch returns '' — Riko can't guess a download for a platform
    Soloist doesn't publish."""
    return ARCHIVE_URLS.get((arch or platform.machine() or "").strip().lower(),
                            "")


def parse_version_output(text: str) -> dict:
    """Parse `soloist --version` output. Returns {version, build_ts,
    build_date (date), expiry (date = build_date + 90 days)} or {} when the
    text isn't a Soloist version line (a wrong-architecture binary prints
    something else and must never pass verification)."""
    m = _VERSION_RE.search(str(text or ""))
    if not m:
        return {}
    try:
        build_date = datetime.strptime(m.group(3), "%Y%m%d").date()
    except ValueError:
        return {}
    return {"version": m.group(1),
            "build_ts": int(m.group(2)),
            "build_date": build_date,
            "expiry": build_date + timedelta(days=BUILD_TTL_DAYS)}


def days_left(expiry, today=None) -> int:
    """Whole days between today and the build's expiry date (negative =
    already expired)."""
    if not isinstance(expiry, date):
        return -1
    return (expiry - (today or date.today())).days


# -- paths (never the repo, never the owner's soloist defaults) --------------

def _data_root(cfg_value="") -> Path:
    v = str(cfg_value or os.environ.get("RIKO_SOLOIST_HOME") or "").strip()
    if v:
        return Path(v).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base).expanduser() / "riko" / "soloist"


def _cache_dir(cfg_value="") -> Path:
    v = str(cfg_value or os.environ.get("RIKO_SOLOIST_CACHE") or "").strip()
    if v:
        return Path(v).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base).expanduser() / "riko" / "soloist"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    base = os.environ.get("RIKO_CONFIG_DIR") or str(Path(base) / "riko")
    return Path(base).expanduser()


def key_path() -> Path:
    return _config_dir() / "soloist.key"


# -- player selection ---------------------------------------------------------

def select_player(music_cfg: dict | None) -> str:
    """desktop.music.player -> 'soloist'|'spotifyd'|'external'|'off'.
    Back-compat: with `player` absent, legacy `spotifyd:` maps
    managed->soloist, external->external, off->off; with NEITHER key the
    default is soloist (docs/SOLOIST.md). A bad `player` value is off."""
    cfg = music_cfg or {}
    v = str(cfg.get("player") or "").strip().lower()
    if v:
        return v if v in ("soloist", "spotifyd", "external", "off") else "off"
    legacy = str(cfg.get("spotifyd") or "").strip().lower()
    if legacy == "managed":
        return "soloist"
    if legacy == "external":
        return "external"
    if legacy == "off":
        return "off"
    return "soloist"


# -- key ----------------------------------------------------------------------

def check_key() -> str:
    """'' when the key file is usable; else the exact owner-facing fix-it
    text. The key's VALUE never appears in any returned text."""
    p = key_path()
    try:
        st = p.stat()
    except OSError:
        return KEY_SETUP_TEXT
    if not st.st_size:
        return KEY_SETUP_TEXT
    if st.st_mode & 0o077:
        return (f"the Soloist key file {p} is readable by group/other — "
                "Riko won't use it. Fix it with: "
                f"chmod 600 {p}")
    try:
        if not p.read_text(encoding="utf-8").strip():
            return KEY_SETUP_TEXT
    except OSError as e:
        return f"can't read the Soloist key file {p}: {e}"
    return ""


def _read_key() -> str:
    try:
        return key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def redact_key(text, key: str):
    """Scrub a secret's value out of any text before it surfaces (tray,
    logs, exceptions). Returns (scrubbed_text, hit_count)."""
    key = str(key or "")
    text = str(text if text is not None else "")
    if not key:
        return text, 0
    n = text.count(key)
    if n:
        text = text.replace(key, REDACTION)
    return text, n


# -- WebSocket API (official local protocol, no auth: loopback only) ----------

class _DefaultConn:
    """websockets>=14 wants its sync connect() used as a context manager;
    this wraps one into the plain {send/recv/close} seam the code below
    (and the tests' fakes) speak."""

    def __init__(self, uri: str):
        from websockets.sync.client import connect
        self._cm = connect(uri, close_timeout=0.5)
        self._conn = self._cm.__enter__()

    def send(self, data: str) -> None:
        self._conn.send(data)

    def recv(self, timeout: float | None = None):
        return self._conn.recv(timeout=timeout)

    def close(self) -> None:
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass


class SoloistWs:
    """Tiny sync client for Soloist's local WebSocket API: endpoint
    discovery via <data-dir>/ws.addr + ws.port (the daemon writes them on
    start with `--ws 127.0.0.1:0` and removes them on shutdown), and the
    JSON text-frame protocol from the official docs (get_state ->
    playback_state, get_auth_state -> auth_state, set_volume ->
    volume_changed). Every failure path returns None/False: the API is
    best-effort, never fatal."""

    def __init__(self, data_dir, *, connect=None, timeout: float = 2.0):
        self.data_dir = Path(data_dir)
        self.timeout = float(timeout)
        self._connect = connect

    def endpoint(self):
        """('127.0.0.1', port) from the runtime files, but only when the
        bind address is loopback — the API has NO authentication at all."""
        try:
            addr = (self.data_dir / "ws.addr").read_text().strip()
            port = int((self.data_dir / "ws.port").read_text().strip())
        except (OSError, ValueError):
            return None
        if addr not in ("127.0.0.1", "localhost", "::1") or not 0 < port < 65536:
            return None
        return addr, port

    def available(self) -> bool:
        return self.endpoint() is not None

    def _open(self, ep):
        uri = f"ws://{ep[0]}:{ep[1]}"
        return (self._connect or _DefaultConn)(uri)

    def _exchange(self, want: str, command: dict):
        ep = self.endpoint()
        if ep is None:
            return None
        conn = None
        try:
            conn = self._open(ep)
            conn.send(json.dumps({"type": "command", **command}))
            deadline = time.monotonic() + self.timeout
            for _ in range(16):               # bounded: never hang the tray
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                msg = json.loads(conn.recv(timeout=left))
                if isinstance(msg, dict) and msg.get("type") == want:
                    return msg
                if isinstance(msg, dict) and msg.get("type") == "error":
                    return None
        except Exception:
            return None
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        return None

    def playback_state(self) -> dict | None:
        return self._exchange("playback_state", {"command": "get_state"})

    def open(self):
        """Open a LONG-LIVED connection to the local API (the ticker's
        event feed needs one; _exchange is single-shot). None when the
        endpoint files are missing/not loopback — same no-fatal contract."""
        ep = self.endpoint()
        if ep is None:
            return None
        try:
            return self._open(ep)
        except Exception:
            return None

    def auth_state(self) -> dict | None:
        return self._exchange("auth_state", {"command": "get_auth_state"})

    def logged_in(self):
        st = self.auth_state()
        return None if st is None else bool(st.get("logged_in"))

    def is_playing(self) -> bool:
        st = self.playback_state()
        return bool(st) and st.get("status") == "playing"

    def set_volume(self, percent: int) -> bool:
        ep = self.endpoint()
        if ep is None:
            return False
        conn = None
        try:
            conn = self._open(ep)
            conn.send(json.dumps({"type": "command", "command": "set_volume",
                                  "volume": int(percent)}))
            # drain until the echo lands or we run out of patience
            deadline = time.monotonic() + self.timeout
            for _ in range(8):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                msg = json.loads(conn.recv(timeout=left))
                if isinstance(msg, dict) and msg.get("type") in (
                        "volume_changed", "command_result"):
                    return True
        except Exception:
            return False
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        return False


# ── ticker fast path: the local playback-event feed ─────────────────────────

def _entity_name(entity) -> str:
    """decorations.identity.name of an entity envelope ('' when absent)."""
    if not isinstance(entity, dict):
        return ""
    return str(((entity.get("decorations") or {}).get("identity") or {})
               .get("name") or "")


def _entity_field(entity, *path, default):
    node = entity
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return node if node is not None else default


def soloist_snapshot(state: dict) -> dict:
    """Accumulated Soloist WS state → the compact now_playing shape the
    hub's now_playing event uses (docs/SPOTIFY_NOTES.md ticker section).
    status idle empties the track fields (that is the hub's hide signal);
    paused/buffering keep the item so the bar can show the paused icon."""
    status = str(state.get("status") or "idle")
    item = state.get("item") if status != "idle" else None
    item = item if isinstance(item, dict) else {}
    dec = item.get("decorations") or {}
    artists = ", ".join(
        n for n in (_entity_name((c or {}).get("entity"))
                    for c in (dec.get("creators") or [])) if n)
    album = ""
    parent = ((dec.get("parent") or {}).get("entity") or {})
    if isinstance(parent, dict) and parent.get("entity_type") == "album":
        album = _entity_name(parent)
    duration = _entity_field(item, "decorations", "playback",
                             "duration_ms", default=0)
    position = state.get("position") or {}
    context = state.get("context") or {}
    return {
        "playing": status == "playing",
        "uri": str(item.get("uri") or ""),
        "track": str(_entity_field(item, "decorations", "identity",
                                   "name", default="")),
        "artists": artists,
        "album": album,
        "duration_ms": int(duration or 0),
        "position_ms": int((position or {}).get("position_ms") or 0),
        "device": str(state.get("device_name") or ""),
        "context_uri": str((context or {}).get("uri") or ""),
        # the WS item's artwork isn't a stable https URL in the docs; art
        # arrives with the hub's now_playing event (art_url) instead
        "art_url": "",
    }


class SoloistFeed:
    """Long-lived listener on Soloist's WebSocket API for the ticker's
    FAST path (docs/SOLOIST.md + the official WS reference).

    Message names verified against the official docs (not guessed): the
    server pushes `auth_state` on connect, `playback_state` (full
    snapshot) when logged in, then granular events — `track_changed`
    {item}, `playback_changed` {status}, `context_changed` {context},
    `device_changed` {is_active, device_name}, `position_sync` {position}
    — plus volume/options/queue events this feed ignores.

    Accumulated state becomes a now_playing-shaped snapshot
    (`soloist_snapshot`) and `on_snapshot(snapshot)` fires ONLY on a real
    change (uri/playing dedupe) and ONLY while Soloist is the active
    Connect device — when playback lives on another device the hub's own
    path is the truth and a local 'idle' would flicker the ticker. The
    caller (the desktop session) forwards to the page and nudges the hub.

    Daemon thread; `ws` is a SoloistWs (endpoint discovery + the
    injectable `connect` seam its tests feed). Everything is best-effort:
    no endpoint, dropped connection or protocol noise = silent retry,
    never fatal, never touches Qt."""

    def __init__(self, ws, on_snapshot, *, log=print, retry_s: float = 2.0,
                 recv_timeout: float = 2.0):
        self.ws = ws
        self.on_snapshot = on_snapshot
        self.log = log
        self.retry_s = float(retry_s)
        self.recv_timeout = float(recv_timeout)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = {}
        self._sig: tuple | None = None

    # -- state machine (pure enough to drive straight from tests) ----------
    def reset(self) -> None:
        self._state = {}

    def feed(self, msg) -> bool:
        """One server message; True when on_snapshot fired. Public so
        tests drive the exact official message shapes."""
        if not isinstance(msg, dict):
            return False
        kind = str(msg.get("type") or "")
        st = self._state
        if kind == "auth_state":
            st["logged_in"] = bool(msg.get("logged_in"))
            st["is_active"] = bool(msg.get("is_active"))
            st["device_name"] = str(msg.get("device_name")
                                    or st.get("device_name") or "")
            if not st["logged_in"]:
                st["status"] = "idle"
                st["item"] = None
                st["context"] = None
            return False
        if kind == "device_changed":
            st["is_active"] = bool(msg.get("is_active"))
            st["device_name"] = str(msg.get("device_name")
                                    or st.get("device_name") or "")
            return False     # transfers belong to the hub path; no local paint
        if kind == "playback_state":
            st["status"] = str(msg.get("status") or "idle")
            st["item"] = msg.get("item")
            st["context"] = msg.get("context")
            st["position"] = msg.get("position")
            if "is_active" in msg:
                st["is_active"] = bool(msg.get("is_active"))
        elif kind == "track_changed":
            st["item"] = msg.get("item")
        elif kind == "playback_changed":
            st["status"] = str(msg.get("status") or "idle")
        elif kind == "context_changed":
            st["context"] = msg.get("context")
        elif kind == "position_sync":
            st["position"] = msg.get("position")
        else:
            return False     # volume/options/queue/command_result/error: mute
        if not st.get("is_active"):
            return False     # another device holds playback: the hub paints
        snap = soloist_snapshot(st)
        sig = (snap["uri"], snap["playing"])
        if sig == self._sig:
            return False
        self._sig = sig
        try:
            self.on_snapshot(dict(snap))
        except Exception as e:
            try:
                self.log(f"[music] soloist ticker feed callback failed: {e}")
            except Exception:
                pass
        return True

    # -- thread lifecycle ----------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run,
                                            name="soloist-ticker-feed",
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def run(self) -> None:
        while not self._stop.is_set():
            conn = self.ws.open() if hasattr(self.ws, "open") else None
            if conn is None:
                self.reset()           # no player: state unknown until next connect
                self._stop.wait(self.retry_s)
                continue
            try:
                while not self._stop.is_set():
                    try:
                        raw = conn.recv(timeout=self.recv_timeout)
                    except TimeoutError:
                        continue       # quiet is normal; poll the stop flag
                    except Exception:
                        break          # dropped connection: reconnect
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    self.feed(msg)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            self.reset()               # fresh auth_state/playback_state after
                                       # a reconnect; _sig survives (no re-paint)


class SoloistDuck:
    """Lower Soloist's volume while she speaks, restore after — the
    WebSocket replacement for MprisDuck (same duck/restore/on_speak
    interface). If the WS isn't discoverable every call is a silent no-op,
    exactly like the dbus-less MprisDuck.

    Live bug 2026-11-12 (volume dipped on the owner's PHONE and stayed
    there): Soloist's set_volume rides the Spotify Connect SESSION, not the
    Soloist device — while playback lived on his phone, duck and restore
    both landed there, late and out of order, and the restore lost the
    race. So: duck ONLY while Soloist itself is the active, playing device
    (from its WS state); otherwise touch nothing. A ducked level captured
    mid-session is also poison, so the pre-duck level is captured once per
    speech run (nested/overlapping speak events never overwrite it), and
    restore CONFIRMS through the WS state, resending once when the reported
    volume is still the ducked level."""

    def __init__(self, fraction: float = DEFAULT_DUCK_FRACTION, *, ws=None):
        try:
            self.fraction = min(1.0, max(0.0, float(fraction)))
        except (TypeError, ValueError):
            self.fraction = DEFAULT_DUCK_FRACTION
        self.ws = ws
        self._saved = None                     # volume% restored on restore()
        self._ducked = None                    # the level restore() verifies against
        self._runs = 0                         # nested speak(True) depth

    def available(self) -> bool:
        return bool(self.ws) and self.ws.available()

    def _active_playing(self) -> dict | None:
        """Soloist's playback_state, but only when Soloist IS the active
        Connect device and playback is running — else None (someone else
        owns the session: its volume knob is not ours to turn)."""
        if self.ws is None:
            return None
        st = self.ws.playback_state() or {}
        if not st:
            return None
        active = st.get("is_active")
        if active is None:                     # some state frames omit it
            authf = getattr(self.ws, "auth_state", None)
            active = (authf() or {}).get("is_active") if authf else None
        if not bool(active):
            return None
        if str(st.get("status") or "").lower() != "playing":
            return None
        return st

    def duck(self) -> None:
        ws = self.ws
        st = self._active_playing()
        if st is None or ws is None:           # not our device / not playing
            return
        self._runs += 1
        if self._saved is not None:            # nested speech: keep the level
            return
        try:
            vol = float(st.get("volume") or 0)
        except (TypeError, ValueError):
            return
        ducked = max(0, min(100, round(vol * self.fraction)))
        if ws.set_volume(ducked):
            self._saved = vol
            self._ducked = ducked

    def restore(self) -> None:
        if self._runs > 0:                     # speech runs still open
            self._runs -= 1
            if self._runs > 0:
                return
        if self._saved is None:
            return
        vol, ducked, self._saved, self._ducked = \
            self._saved, self._ducked, None, None
        if self.ws is None:
            return
        target = max(0, min(100, round(vol)))
        self.ws.set_volume(target)
        # confirm through the WS state: Connect/WS echo can drop a write.
        # Only resend when the reported volume is still the DUCKED level —
        # anything else (even the user's own change) stays untouched.
        try:
            time.sleep(0.05)
            reported = float((self.ws.playback_state() or {}).get("volume") or 0)
        except (TypeError, ValueError):
            return
        if ducked is not None and abs(reported - ducked) <= abs(reported - target):
            self.ws.set_volume(target)

    def restore_now(self) -> None:
        """Shutdown hook: drop any nesting bookkeeping and restore whatever
        duck is still active — the process is leaving, speech runs are over."""
        self._runs = 0
        self.restore()

    def on_speak(self, speaking: bool) -> None:
        """Session.on_speak hook: True = she started, False = she stopped."""
        (self.duck if speaking else self.restore)()


class CompositeDuck:
    """Soloist's WS duck when its endpoint files exist, else the MPRIS duck
    (the `player: external` case — an externally-run spotifyd still has
    MPRIS). Same on_speak hook, still silent no-op all round."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def on_speak(self, speaking: bool) -> None:
        duck = self.primary if self.primary.available() else self.fallback
        duck.on_speak(speaking)

    def restore_now(self) -> None:
        """Shutdown: restore BOTH — whichever ducked (availability can flip
        mid-run), and an empty duck state is a no-op either way."""
        for duck in (self.primary, self.fallback):
            try:
                duck.restore_now()
            except Exception:  # noqa: BLE001 — shutdown path, never raise
                pass


# -- auto-update ---------------------------------------------------------------

def _default_http(url: str, method: str = "GET", timeout: float = 30.0):
    """The real seam: one https request to the official CDN. Response is
    read fully for GET (the caller caps the size via Content-Length, then
    streams with a hard cap below)."""
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "riko-desktop/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # urllib follows redirects: the FINAL url must still be the official
        # CDN over https, or nothing from it is trusted (docs/SOLOIST.md).
        final = urllib.parse.urlparse(resp.geturl())
        if final.scheme != "https" or final.hostname != CDN_HOST:
            raise OSError(f"redirected off the official CDN to "
                          f"{final.scheme}://{final.hostname}")
        status = getattr(resp, "status", 200)
        headers = {k.lower(): v for k, v in dict(resp.headers).items()}
        data = b""
        if method != "HEAD":
            data = resp.read(DOWNLOAD_MAX_BYTES + 1)
        return status, headers, data


def _default_runner(argv, timeout):
    return subprocess.run(list(argv), capture_output=True, text=True,
                          timeout=timeout)


class SoloistUpdater:
    """Official-CDN-only installer. release.json holds {etag,
    last_modified, version, build_date, installed_at, sha256}; a check
    HEADs the archive and downloads only on a difference (or a missing
    binary). A new binary is verified with `<new> --version` (must parse
    and not already be expired) BEFORE the old one is replaced — a failed
    check leaves the old binary exactly where it was."""

    def __init__(self, music_cfg: dict | None = None, *,
                 http=_default_http, runner=_default_runner,
                 log=print, clock=time.time):
        cfg = music_cfg or {}
        solo = cfg.get("soloist") or {}
        self.root = _data_root(str(cfg.get("soloist_home") or ""))
        self.bin_path = self.root / "bin" / "soloist"
        self.release_path = self.root / "release.json"
        self.url = archive_url()
        self.http = http
        self.runner = runner
        self.log = log
        self.clock = clock
        self.auto_update = bool(solo.get("auto_update", True))
        self._failed_kinds: set[str] = set()    # log once per failure kind

    def _note(self, line: str) -> None:
        try:
            self.log(line)
        except Exception:
            pass

    def _fail(self, kind: str, reason: str) -> dict:
        if kind not in self._failed_kinds:
            self._failed_kinds.add(kind)
            self._note(f"[music] soloist update: {reason}")
        return {"ok": False, "updated": False, "kind": kind, "reason": reason}

    # -- metadata -----------------------------------------------------------
    def release_info(self) -> dict:
        try:
            info = json.loads(self.release_path.read_text(encoding="utf-8"))
            return info if isinstance(info, dict) else {}
        except (OSError, ValueError):
            return {}

    def installed_version(self) -> dict:
        """Parsed version info for the binary Riko installed: release.json
        first (no subprocess), falling back to running `<binary> --version`
        (a binary the owner dropped in place by hand still shows up)."""
        if not self.bin_path.exists():
            return {}
        info = self.release_info()
        parsed = parse_version_output(str(info.get("version") or "")
                                      and f"soloist {info['version']} build "
                                      f"0 ({info.get('build_date') or ''})")
        if parsed.get("expiry"):
            return parsed
        try:
            r = self.runner([str(self.bin_path), "--version"],
                            timeout=VERSION_TIMEOUT_S)
            return parse_version_output(getattr(r, "stdout", "") or "")
        except Exception:
            return {}

    # -- the check ------------------------------------------------------------
    def check(self, force: bool = False) -> dict:
        """HEAD the CDN; download+install on a difference. {ok, updated,
        version?, reason?}. Never raises, never touches a running player —
        restart decisions live in SoloistPlayer.check_update()."""
        if not self.url:
            return self._fail("arch",
                              f"no official Soloist build for "
                              f"{platform.machine()!r} — not downloading")
        have_binary = self.bin_path.exists()
        info = self.release_info()
        if not force and have_binary and not self.auto_update:
            return {"ok": True, "updated": False, "skipped": "auto_update off"}
        try:
            status, headers, _ = self.http(self.url, "HEAD")
        except Exception as e:
            return self._fail("network", f"HEAD {self.url} failed: {e}")
        if status != 200:
            return self._fail("head", f"the CDN answered HTTP {status}")
        etag = str(headers.get("etag") or "")
        last_modified = str(headers.get("last-modified") or "")
        if (have_binary and not force
                and etag == str(info.get("etag") or "")
                and last_modified == str(info.get("last_modified") or "")):
            return {"ok": True, "updated": False, "reason": "up to date"}
        # content changed (or no binary at all): full download
        try:
            size = int(str(headers.get("content-length") or "0") or 0)
        except ValueError:
            size = 0
        if size > DOWNLOAD_MAX_BYTES:
            return self._fail("size", f"the archive is {size} bytes — over "
                                      f"the {DOWNLOAD_MAX_BYTES} cap")
        try:
            status, _, blob = self.http(self.url, "GET")
        except Exception as e:
            return self._fail("network", f"download failed: {e}")
        if status != 200 or not blob:
            return self._fail("download", f"the CDN answered HTTP {status}")
        if len(blob) > DOWNLOAD_MAX_BYTES:
            return self._fail("size", "the downloaded archive is over the cap")
        return self._install(blob, etag, last_modified)

    def _extract_soloist(self, blob: bytes, dest: Path) -> dict:
        """Pull ONLY the `soloist` member out of the tar.gz. Rejects
        absolute paths, '..', symlinks/hardlinks, non-regular members and
        oversize entries; the old binary is never touched here."""
        try:
            tar = tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)))
        except (OSError, EOFError, tarfile.TarError) as e:
            return self._fail("archive", f"the archive won't open: {e}")
        with tar:
            for m in tar.getmembers():
                name = str(m.name)
                if name != "soloist":
                    continue
                if m.issym() or m.islnk():
                    return self._fail("archive",
                                      "soloist is a symlink in the archive")
                if not m.isfile():
                    return self._fail("archive",
                                      f"soloist isn't a regular file "
                                      f"({m.type!r})")
                if m.size <= 0 or m.size > DOWNLOAD_MAX_BYTES:
                    return self._fail("archive",
                                      f"soloist entry size {m.size} is bogus")
                fh = tar.extractfile(m)
                if fh is None:
                    return self._fail("archive", "soloist member unreadable")
                data = fh.read(DOWNLOAD_MAX_BYTES + 1)
                if len(data) > DOWNLOAD_MAX_BYTES:
                    return self._fail("archive", "soloist member over cap")
                dest.write_bytes(data)
                return {"ok": True, "data_sha": hashlib.sha256(data).hexdigest()}
        return self._fail("archive", "the archive has no 'soloist' member")

    def _install(self, blob: bytes, etag: str, last_modified: str) -> dict:
        bin_dir = self.bin_path.parent
        staging = bin_dir / "staging"
        try:
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self._fail("fs", f"can't prepare {staging}: {e}")
        tmp = staging / "soloist"
        try:
            got = self._extract_soloist(blob, tmp)
            if not got.get("ok"):
                return got
            os.chmod(tmp, 0o755)            # the tar header may carry mode 0
            try:
                r = self.runner([str(tmp), "--version"],
                                timeout=VERSION_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return self._fail("verify", "`soloist --version` timed out")
            except OSError as e:
                return self._fail("verify", f"`soloist --version` won't run: {e}")
            out = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
            parsed = parse_version_output(out)
            if getattr(r, "returncode", 1) != 0 or not parsed:
                return self._fail("verify", "the downloaded binary answered "
                              "`--version` with something that isn't a "
                              "Soloist version "
                              f"({out.strip()[:120] or 'no output'})")
            if days_left(parsed["expiry"]) < 0:
                return self._fail("verify",
                                  f"the downloaded build already expired "
                                  f"({parsed['expiry'].isoformat()})")
            bin_dir.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, self.bin_path)      # atomic, same filesystem
            meta = {"etag": etag, "last_modified": last_modified,
                    "version": parsed["version"],
                    "build_date": parsed["build_date"].isoformat(),
                    "installed_at": int(self.clock()),
                    "sha256": got["data_sha"]}
            meta_tmp = self.release_path.with_suffix(".json.tmp")
            meta_tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            os.replace(meta_tmp, self.release_path)
        except OSError as e:
            return self._fail("fs", f"install failed: {e}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._note(f"[music] soloist updated to {parsed['version']} "
                   f"(build {parsed['build_date'].isoformat()}, expires "
                   f"{parsed['expiry'].isoformat()})")
        return {"ok": True, "updated": True, "version": parsed["version"],
                "build_date": parsed["build_date"].isoformat(),
                "expiry": parsed["expiry"].isoformat()}


# -- the player -----------------------------------------------------------------

class SoloistPlayer:
    """The tray's handle on the Soloist child process — the same
    start/stop/status/result-dict shape as SpotifydPlayer so desktop.py can
    treat both alike (docs/SOLOIST.md). `mode` comes from select_player():
    only 'managed' spawns; 'external' means the owner runs soloist; soloist
    (unlike spotifyd) IS installed by Riko — the official CDN is the only
    source and the archive is never redistributed."""

    kind = "soloist"

    def __init__(self, music_cfg: dict | None, device_name: str = DEFAULT_DEVICE_NAME,
                 *, which=shutil.which, popen=subprocess.Popen, log=print,
                 updater=None, ws=None, clock=time.monotonic):
        cfg = dict(music_cfg or {})
        sel = select_player(cfg)
        self.mode = {"soloist": "managed", "spotifyd": "managed",
                     "external": "external", "off": "off"}[sel]
        self.player_kind = sel
        self.device_name = (str(device_name or "").strip() or DEFAULT_DEVICE_NAME)
        solo = cfg.get("soloist") or {}
        try:
            self.cache_size_mb = int(solo.get("cache_size_mb", DEFAULT_CACHE_SIZE_MB))
        except (TypeError, ValueError):
            self.cache_size_mb = DEFAULT_CACHE_SIZE_MB
        self.extra_args = [str(a) for a in (solo.get("extra_args") or [])]
        self.auto_update = bool(solo.get("auto_update", True))
        iv = str(solo.get("initial_volume") or "").strip()
        self.initial_volume = iv if iv != "" else ""
        self.pipewire_device = str(solo.get("pipewire_device") or "").strip()
        self.root = _data_root(str(cfg.get("soloist_home") or ""))
        self.data_dir = self.root / "data"
        self.binary_override = str(solo.get("binary") or "").strip()
        self.log = log
        self._which = which
        self._popen = popen
        self._clock = clock
        self.updater = updater if updater is not None else SoloistUpdater(cfg, log=log)
        self.ws = ws if ws is not None else SoloistWs(self.data_dir)
        self._proc = None
        self._reader = None
        self._key_value = ""
        self._last_check = 0.0
        self._pending_restart = False
        self._output: collections.deque[str] = collections.deque(maxlen=OUTPUT_RING)

    def _note(self, line: str) -> None:
        try:
            self.log(line)
        except Exception:
            pass

    def _redact(self, text) -> str:
        out, _ = redact_key(text, self._key_value)
        return out

    # -- discovery ------------------------------------------------------------
    def find_binary(self) -> str:
        if self.binary_override:
            return (self.binary_override
                    if os.path.exists(self.binary_override) else "")
        p = self.root / "bin" / "soloist"
        return str(p) if p.exists() else ""

    def key_error(self) -> str:
        return check_key()

    # -- lifecycle --------------------------------------------------------------
    def argv(self, key: str = "<key>") -> list[str]:
        cmd = [self.find_binary() or "soloist",
               "-n", self.device_name,
               "-k", key,
               "-D", str(self.data_dir),
               "-C", str(_cache_dir()),
               # Soloist accepts 0 (unlimited) or >= 100 MB only
               "-z", str(0 if self.cache_size_mb <= 0
                         else max(100, self.cache_size_mb)),
               "-w", "127.0.0.1:0"]
        if str(self.initial_volume) != "":
            cmd += ["-i", str(self.initial_volume)]
        if self.pipewire_device:
            cmd += ["-d", self.pipewire_device]
        return cmd + self.extra_args

    def _spawn(self):
        try:
            proc = self._popen(self.argv(self._key_value),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               text=True,
                               cwd=str(self.root))
        except OSError as e:
            self._proc = None
            return None, f"soloist would not start: {e}"
        self._proc = proc
        self._start_drain(proc)
        return proc, ""

    def _start_drain(self, proc) -> None:
        """Pipe the child's output into the ring buffer on a daemon thread
        (the pipe never blocks); every line is redacted the moment it
        lands, so the key can never surface later."""
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return

        def _drain():
            try:
                for line in stream:
                    line = str(line).strip()
                    if line:
                        self._output.append(self._redact(line))
            except Exception:
                pass
        self._reader = threading.Thread(target=_drain, daemon=True,
                                        name="soloist-output")
        self._reader.start()

    def _last_meaningful(self) -> str:
        lines = [l for l in self._output if l and l.strip()]
        if not lines:
            return ""
        for line in reversed(lines):
            if any(k in line.lower() for k in ("error", "invalid", "failed",
                                               "panic", "expired")):
                return line[:300]
        return lines[-1][:300]

    def running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        return proc.poll() is None

    def version_info(self) -> dict:
        return self.updater.installed_version()

    def status(self) -> dict:
        """For the tray line; the core keys match SpotifydPlayer.status()
        so build_music_menu can treat both players alike. Extra soloist
        keys: version, expiry (ISO date), days_left, key_error, kind."""
        info = self.version_info()
        expiry = info.get("expiry")
        kerr = self.key_error()
        setup = not bool(self.find_binary()) and self.mode == "managed"
        # the WS probe only while running: _refresh() runs on the Qt thread
        # at every menu open and must never wait on a dead endpoint
        login = (self.running() and not kerr and self.ws.logged_in() is False)
        return {"mode": self.mode,
                "running": self.running(),
                "pid": (self._proc.pid
                        if (self._proc is not None and self._proc.poll() is None)
                        else None),
                "binary": self.find_binary(),
                "device_name": self.device_name,
                "setup_needed": setup,
                "login_needed": login,
                "kind": self.player_kind,
                "version": str(info.get("version") or ""),
                "expiry": expiry.isoformat() if expiry else "",
                "days_left": (days_left(expiry) if expiry else None),
                "key_error": kerr,
                "update_pending": self._pending_restart,
                "detail": (kerr or
                           ("Soloist is not installed yet — Riko installs it "
                            "from Spotify's official CDN on the next Start "
                            "or update check." if setup else ""))}

    def start(self) -> dict:
        if self.mode == "off":
            return {"ok": False,
                    "message": "music is switched off "
                               "(character_config.yaml desktop.music.player: "
                               "off)"}
        if self.mode == "external":
            return {"ok": False,
                    "message": "player: external — you start and stop the "
                               "player yourself; Riko only uses it"}
        if self.running():
            return {"ok": True, "message": f"player already running "
                                           f"(pid {self._proc.pid if self._proc else '?'})"}
        kerr = self.key_error()
        if kerr:
            return {"ok": False, "message": kerr}
        self._key_value = _read_key()
        if not self.find_binary():
            if not self.auto_update:
                return {"ok": False,
                        "message": "Soloist is not installed and "
                                   "desktop.music.soloist.auto_update is "
                                   "false — tray's 'Update Soloist now' "
                                   "installs it from the official CDN."}
            res = self.updater.check(force=True)     # install from the CDN
            if not res.get("ok") or not self.find_binary():
                return {"ok": False,
                        "message": f"couldn't install Soloist from "
                                   f"Spotify's CDN: {res.get('reason', 'unknown')}"}
        empty_data = not any(self.data_dir.iterdir()) if self.data_dir.is_dir() else True
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _cache_dir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False,
                    "message": f"can't prepare {self.data_dir}: {e}"}
        self._output.clear()
        proc, err = self._spawn()
        if proc is None or err:
            return {"ok": False, "message": self._redact(err)}
        time.sleep(0.3)                   # a binary that dies instantly is a fail
        code = None if proc.poll() is None else getattr(proc, "returncode", None)
        if code is not None and code != EXPIRED_EXIT:
            # an early exit: soloist's own (redacted) output is the report
            if self._reader is not None:
                self._reader.join(timeout=0.5)
            line = self._last_meaningful()
            self._proc = None
            msg = f"soloist exited right away (code {code})"
            if line:
                msg += f" — last output: {line}"
            if _DATA_DIR_BUSY in line.lower():
                msg += " — another Soloist is already running; Riko won't " \
                       "fight it for the data dir (Stop it, or set " \
                       "player: external)"
            else:
                msg += f" — data dir {self.data_dir}, key file {key_path()}"
            return {"ok": False, "message": self._redact(msg)}
        if code == EXPIRED_EXIT:
            # docs/SOLOIST.md: exit 10 = build expired -> one forced update
            # + one restart attempt, then give up with a clear tray error.
            self._note("[music] soloist exited 10 (build expired) — forcing "
                       "an update")
            self._proc = None
            res = self.updater.check(force=True)
            if not res.get("updated") or not self.find_binary():
                return {"ok": False,
                        "message": "Soloist build expired and the update "
                                   f"failed: {res.get('reason', res.get('kind', 'unknown'))}"}
            self._output.clear()
            proc, err = self._spawn()
            if proc is None or err:
                return {"ok": False, "message": self._redact(err)}
            time.sleep(0.3)
            if proc.poll() is None:
                code = None
            else:
                code = getattr(proc, "returncode", None)
                if code != EXPIRED_EXIT and code is not None:
                    return {"ok": False,
                            "message": f"Soloist build expired and the "
                                       f"update failed: the new build exited "
                                       f"right away (code {code}): "
                                       f"{self._redact(self._last_meaningful())}"}
                self._proc = None
                return {"ok": False,
                        "message": "Soloist build expired and the update "
                                   "failed: the fresh build still exits 10"}
        msg = f"desktop player '{self.device_name}' started (pid {self._proc.pid})"
        logged_in = self.ws.logged_in()
        if logged_in is False or (logged_in is None and empty_data):
            msg += f" — {PAIR_HINT}"
            self._note(f"[music] {PAIR_HINT}")
        return {"ok": True, "message": msg}

    def stop(self) -> dict:
        if self.mode == "off":
            return {"ok": False, "message": "music is switched off"}
        if self.mode == "external":
            return {"ok": False, "message": "player: external — Riko did "
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

    # -- updates -------------------------------------------------------------
    def due(self) -> bool:
        """True when the periodic check is due (driven by the injectable
        clock; the desktop fires it at start and every 6 h)."""
        return (self._clock() - self._last_check) >= CHECK_INTERVAL_S

    def check_update(self) -> dict:
        """Update check + the restart decision (docs/SOLOIST.md): restart
        only while idle — unless the running build expires within 3 days —
        otherwise defer to the next idle moment."""
        self._last_check = self._clock()
        was_running = self.running()
        info = self.version_info()
        expiry = info.get("expiry")
        left = days_left(expiry) if expiry else None
        # never force here: a periodic check that sees the same etag must
        # not re-download 14 MB; `force` belongs to the exit-10 recovery
        # path in start() (expired binary -> trust the CDN over our cache).
        res = self.updater.check()
        if not res.get("updated"):
            if self._pending_restart and was_running and not self.ws.is_playing():
                res = dict(res, restarted=self.restart())
                self._pending_restart = False
            return res
        if left is not None and left < RESTART_SOON_DAYS:
            return dict(res, restarted=self.restart(),
                        note="restarting now: the running build expires "
                             f"within {RESTART_SOON_DAYS} days")
        if self.ws.is_playing():
            self._pending_restart = True
            self._note("[music] soloist update installed; restart deferred "
                       "until playback goes quiet")
            return dict(res, deferred=True)
        return dict(res, restarted=self.restart())

    def restart(self) -> bool:
        """Recover a player we manage: stop, then update-aware start."""
        stopped = self.stop()
        started = self.start()
        ok = stopped.get("ok") and started.get("ok")
        return bool(ok)

    def update_now(self) -> dict:
        """The tray's 'Update Soloist now'. Install/update first, then
        restart respecting the same idle rule the periodic check uses."""
        res = self.check_update()
        if res.get("ok"):
            if not res.get("updated") and not res.get("restarted"):
                res = dict(res, message="Soloist is already up to date")
            return res
        return dict(res, message=res.get("reason", "update failed"))
