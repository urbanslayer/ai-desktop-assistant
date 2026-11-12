"""MPRIS2 export so media widgets see her music (docs/SOLOIST.md).

Soloist has no MPRIS of its own (the official docs never mention it — it is
a headless Connect player), so a dux-shell-style bar (DankMaterialShell-like
Quickshell bars, any playerctl-based widget) shows nothing while she plays.
Riko therefore exports `org.mpris.MediaPlayer2.riko` on the session bus with
the SAME reconciled now-playing state that feeds the page ticker
(docs/SPOTIFY_NOTES.md): hub now_playing events + the Soloist WS fast path.
Transport (PlayPause/Next/Previous/Seek...) goes back through the hub
(hub_client.spotify_control -> POST /api/spotify/control).

character_config.yaml desktop.music.mpris:
    riko  — default: advertise only while Riko's own Connect device holds
            playback (the bar then behaves like a local player for her own
            audio, never steals the widget from another device)
    any   — mirror whatever Spotify plays on ANY device (as a remote)
    off   — never export
Legacy `player: spotifyd` never exports: spotifyd ships its own
org.mpris.MediaPlayer2.spotifyd and two players confuse the bars.

When the name is registered but nothing qualifies (mode `riko` and playback
on another device), the name STAYS and reports PlaybackStatus "Stopped" with
empty metadata: bars keyed on NameOwnerChanged can cache stale players when
names appear/disappear (and every re-registration is a re-discovery race),
while Stopped + {} is the universal "render nothing" state — and a later
Play paints instantly because no re-registration round trip is needed.

Why jeepney is the one new dependency: MPRIS2 pins mpris:length, Position
and the Seeked argument to int64 and mpris:trackid to an object path inside
the a{sv} Metadata. PySide6 QtDBus — the Qt binding already in the
project — cannot express that cleanly: QDBusAbstractAdaptor needs
Q_CLASSINFO("D-Bus Interface", ...), which PySide6 does not expose; a plain
QObject registers under at most ONE auto-derived interface per object path;
and every a{sv} member is marshalled from the narrowest Python int, so a
small mpris:length leaves as int32 where the spec says `x` (verified on the
wire with dbus-monitor). dbus-python is a C extension over libdbus/GLib the
locked venv does not carry at all. jeepney is pure Python, MIT, zero-
dependency, and marshals every variant member from an explicit
(signature, value) pair; its socket is driven here by a stdlib selectors
loop on a daemon thread — the same loop jeepney's own docs use.

Everything is best-effort like the rest of the music stack: no session bus,
a name a second Riko instance already owns, or protocol noise log once and
never break audio or the tray.

Threading: `update()` is called from the hub-events follower and the
Soloist feed threads (this module imports no Qt at all). Incoming calls are
dispatched on the bus thread and anything that would wait on the hub or the
Soloist WS leaves it immediately on a worker thread — one Spotify round
trip must never stall PropertiesChanged for the whole bus.

Test seams: `control(action, value)` (defaults to hub_client), `ws`
(SoloistWs: volume read/set_volume), `log`, `clock` (position
extrapolation), `connector` (opens the jeepney connection). Tests run
against a throwaway PRIVATE dbus-daemon — never the owner's session bus.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

MPRIS_SERVICE = "org.mpris.MediaPlayer2.riko"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
IDENTITY = "Riko"

POSITION_JUMP_US = 1_000_000        # >1 s between snapshots counts as a jump
VOLUME_REFRESH_S = 5.0              # min gap between Soloist WS volume reads
RECEIVE_TICK_S = 0.5                # receive-loop poll (stop-flag friendly)

_DESKTOP_FILE_NAMES = ("riko.desktop", "riko-desktop.desktop")

INTROSPECT_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="Fullscreen" type="b" access="read"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Play"/>
    <method name="Pause"/>
    <method name="Stop"/>
    <method name="Next"/>
    <method name="Previous"/>
    <method name="PlayPause"/>
    <method name="Seek"><arg type="x" direction="in"/></method>
    <method name="SetPosition">
      <arg type="o" direction="in"/>
      <arg type="x" direction="in"/>
    </method>
    <method name="OpenUri"><arg type="s" direction="in"/></method>
    <signal name="Seeked"><arg type="x" name="Position"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>"""


# ── pure helpers (drive straight from tests) ─────────────────────────────────

def resolve_mode(music_cfg: dict | None) -> str:
    """desktop.music.mpris -> 'riko'|'any'|'off'; default 'any' (live
    2026-11-12: with `riko` the bar's media widget ignored phone playback —
    the export stays Stopped while Spotify plays on another device, so
    dux-shell-style bars picked some random Firefox player instead. `any`
    mirrors whatever Spotify plays on ANY device, as a remote control);
    `riko` (own-device only) and `off` remain selectable; a bad value is
    'off' (same fail-closed habit as select_player())."""
    v = str((music_cfg or {}).get("mpris") or "").strip().lower()
    if not v:
        return "any"
    return v if v in ("riko", "any", "off") else "off"


def should_publish(mode: str, player_kind: str) -> bool:
    """`off` never publishes; `player: off` (music stack off) never does
    either; and legacy `player: spotifyd` never does — spotifyd already
    exports org.mpris.MediaPlayer2.spotifyd and two players confuse the
    bars. `external` publishes: the riko-mode device gate keeps the export
    honest even when the user's own Spotify client is around."""
    return mode != "off" and player_kind not in ("spotifyd", "off")


def desktop_entry() -> str:
    """The .desktop file name WITHOUT extension when one exists on this
    system (MPRIS DesktopEntry), else ''. Checks the XDG application dirs
    only — Riko doesn't install its own .desktop."""
    home = Path.home()
    dirs = [home / ".local" / "share" / "applications"]
    xdg = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs.extend(Path(p) / "applications" for p in xdg.split(os.pathsep) if p)
    for d in dirs:
        for name in _DESKTOP_FILE_NAMES:
            try:
                if (d / name).is_file():
                    return name[: -len(".desktop")]
            except OSError:
                continue
    return ""


def trackid_path(uri: str) -> str:
    """Spotify URI -> a valid MPRIS trackId object path. The id is kept as
    is when path-safe, else sanitised; the empty URI is /NoTrack."""
    raw = str(uri or "")
    ident = raw.split(":")[-1] if raw.startswith("spotify:") else raw
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in ident).strip("_")
    if not safe:
        return MPRIS_PATH + "/TrackList/NoTrack"
    if safe[0].isdigit():
        safe = "t" + safe
    return f"{MPRIS_PATH}/TrackList/Track/{safe}"


def track_url(uri: str) -> str:
    """open.spotify.com link for a track URI ('' for anything else)."""
    raw = str(uri or "")
    if raw.startswith("spotify:track:"):
        return "https://open.spotify.com/track/" + raw.split(":")[-1]
    return ""


def album_art_url(item_or_snap: dict) -> str:
    """Largest https album-art URL out of a Spotify snapshot's art_url
    (already filtered by the hub) or a raw currently-playing item — this
    module only re-checks the scheme so an http URL can never leak out."""
    url = str((item_or_snap or {}).get("art_url") or "")
    return url if url.startswith("https://") else ""


def qualifies(snap: dict, mode: str, device_name: str) -> bool:
    """Does this snapshot make Riko's player the one to advertise?
    `any`: any real state qualifies. `riko`: only while Riko's own Connect
    device is the active playback device (the hub snapshot names it; the
    Soloist WS fast path only fires while Soloist is active)."""
    if mode not in ("riko", "any"):
        return False
    snap = snap or {}
    if not (str(snap.get("uri") or "") or snap.get("playing")):
        return False
    if mode == "any":
        return True
    want = str(device_name or "").strip().lower()
    return str(snap.get("device") or "").strip().lower() == want


def playback_status(snap: dict) -> str:
    snap = snap or {}
    if snap.get("playing"):
        return "Playing"
    if str(snap.get("uri") or ""):
        return "Paused"
    return "Stopped"


def metadata(snap: dict) -> dict:
    """now_playing snapshot -> MPRIS Metadata as jeepney variant members
    (signature, value). Only keys with real values are included; bars
    tolerate missing xesam keys far better than empty strings."""
    snap = snap or {}
    uri = str(snap.get("uri") or "")
    if not uri:
        return {}
    meta: dict = {"mpris:trackid": ("o", trackid_path(uri))}
    title = str(snap.get("track") or "")
    if title:
        meta["xesam:title"] = ("s", title)
    artists = [a.strip() for a in str(snap.get("artists") or "").split(",")
               if a.strip()]
    if artists:
        meta["xesam:artist"] = ("as", artists)
    album = str(snap.get("album") or "")
    if album:
        meta["xesam:album"] = ("s", album)
    try:
        length_us = int(snap.get("duration_ms") or 0) * 1000
    except (TypeError, ValueError):
        length_us = 0
    if length_us > 0:
        meta["mpris:length"] = ("x", length_us)
    art = album_art_url(snap)
    if art:
        meta["mpris:artUrl"] = ("s", art)
    url = track_url(uri)
    if url:
        meta["xesam:url"] = ("s", url)
    return meta


# ── the publisher ─────────────────────────────────────────────────────────────

class RikoMprisPublisher:
    """Owns org.mpris.MediaPlayer2.riko on the session bus and mirrors the
    reconciled now-playing state into it. All bus traffic runs on one daemon
    thread (jeepney socket + selectors); update() is thread-safe; hub and WS
    round trips always leave that thread immediately. Every failure is a
    logged no-op — an absent bus (CI, ssh session) never breaks the tray."""

    def __init__(self, mode: str = "any", device_name: str = "Riko", *,
                 control=None, ws=None, log=print, clock=time.monotonic,
                 connector=None):
        self.mode = mode if mode in ("riko", "any") else "off"
        self.device_name = device_name
        self._control = control            # None -> hub_client.spotify_control
        self._ws = ws
        self.log = log
        self._clock = clock
        self._connector = connector        # None -> jeepney.io.threading
        self._lock = threading.RLock()
        self._snap: dict = {}
        self._anchor = 0.0                 # clock() when _snap landed
        self._volume = 1.0
        self._vol_next = 0.0               # throttle for WS volume reads
        self._status = "Stopped"
        self._uri = ""
        self._seen_pos_us = -1             # last position we extrapolated
        self._conn = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._owned = False
        self._noted: set[str] = set()      # log each failure kind once

    # -- lifecycle ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._owned)

    def start(self) -> bool:
        """Open the bus connection, own the name, start the dispatch loop.
        Idempotent; False (with one log line) when the bus is missing or the
        name belongs to another player."""
        if self.mode == "off" or self._owned:
            return self._owned
        try:
            from jeepney.io.threading import open_dbus_connection
        except Exception as e:      # jeepney missing: same no-bus contract
            self._note("import", f"MPRIS disabled (jeepney unavailable): {e}")
            return False
        try:
            conn = (self._connector or open_dbus_connection)(bus="SESSION")
            try:
                own = self._request_name(conn)
            except Exception as e:
                conn.close()
                self._note("name", f"could not claim {MPRIS_SERVICE}: {e}")
                return False
            if own != 1:
                conn.close()
                self._note("name", f"{MPRIS_SERVICE} is already taken "
                                   "(another Riko?); not exporting MPRIS")
                return False
            self._conn = conn
            self._owned = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name="riko-mpris", daemon=True)
            self._thread.start()
            self._refresh_volume()
            return True
        except Exception as e:      # no session bus (CI, bare tty): silent-ish
            self._note("bus", f"MPRIS export unavailable (no session bus?): "
                              f"{e}")
            self._conn = None
            self._owned = False
            return False

    def stop(self) -> None:
        """Release the name and close the connection (tray toggle + quit).
        Safe to call twice, from any thread."""
        self._stop.set()
        conn, self._conn = self._conn, None
        thread, self._thread = self._thread, None
        self._owned = False
        if conn is not None:
            try:
                conn.interrupt()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=2.0)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def set_enabled(self, on: bool) -> bool:
        """Tray checkbox: take the name down / back up without changing the
        configured mode."""
        return self.start() if on else (self.stop(), False)[1]

    def _note(self, kind: str, line: str) -> None:
        if kind not in self._noted:
            self._noted.add(kind)
            try:
                self.log(f"[music] {line}")
            except Exception:
                pass

    _OWN_SERIAL = 0x5249  # fixed serial for the RequestName round trip

    def _request_name(self, conn) -> int:
        """RequestName with a hand-rolled round trip: jeepney's threading
        module hands out a bare DBusConnection (no router), and nothing else
        is in flight yet on a fresh connection, so matching the reply by its
        serial is safe. Returns the bus's own flag (1 = we own the name)."""
        from jeepney.bus_messages import message_bus
        from jeepney.low_level import HeaderFields, MessageType
        msg = message_bus.RequestName(MPRIS_SERVICE, 0x1 | 0x4)
        conn.send(msg, serial=self._OWN_SERIAL)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                r = conn.receive(timeout=10.0)
            except TimeoutError:
                continue
            hdr = r.header
            if hdr.fields.get(HeaderFields.reply_serial) != self._OWN_SERIAL:
                continue
            if hdr.message_type == MessageType.method_return:
                return int(r.body[0])
            raise RuntimeError(str(r.body[0] if r.body else "bus error"))
        raise TimeoutError("bus did not answer RequestName")

    # -- state (ticker's reconciled snapshot) ---------------------------------
    def update(self, snap) -> None:
        """One reconciled now_playing snapshot (session.on_music_state).
        Callable from any thread, before start() too; never raises."""
        if not isinstance(snap, dict):
            return
        with self._lock:
            old_snap = self._snap
            snap = dict(snap)
            if not snap.get("art_url") and old_snap and \
                    snap.get("uri") and snap.get("uri") == old_snap.get("uri"):
                # the Soloist WS fast path knows no stable https art URL;
                # keep the artwork the hub snapshot brought for this track
                # so fast-path beats never blank the bar's cover art
                snap["art_url"] = old_snap.get("art_url") or ""
            self._snap = snap
            self._anchor = self._clock()
            signals = self._changes_locked(old_snap)
        for send in signals:
            send()
        if self._status != "Stopped":
            self._refresh_volume()

    def _position_us_locked(self, snap: dict | None = None) -> int:
        snap = self._snap if snap is None else snap
        try:
            base = int(snap.get("position_ms") or 0) * 1000
            dur = int(snap.get("duration_ms") or 0) * 1000
        except (TypeError, ValueError):
            return 0
        if base <= 0 and dur <= 0:
            return 0
        if not snap.get("playing") or not qualifies(snap, self.mode,
                                                    self.device_name):
            return max(base, 0)
        pos = base + int((self._clock() - self._anchor) * 1_000_000)
        return min(pos, dur) if dur > 0 else max(pos, 0)

    def _effective_locked(self, snap: dict) -> tuple[str, dict]:
        if qualifies(snap, self.mode, self.device_name):
            return playback_status(snap), metadata(snap)
        return "Stopped", {}

    def _changes_locked(self, old_snap: dict) -> list:
        """Diff old -> new effective state; returns 0-2 signal emitters
        (PropertiesChanged, then Seeked on track jumps)."""
        new_status, new_meta = self._effective_locked(self._snap)
        old_status, _ = self._effective_locked(old_snap)
        new_uri = str(self._snap.get("uri") or "") if new_status != "Stopped" \
            else ""
        old_uri = self._uri
        emitters: list = []
        changed: dict = {}
        if (new_status, new_uri) != (old_status, old_uri):
            changed["PlaybackStatus"] = ("s", new_status)
            changed["Metadata"] = ("a{sv}", new_meta)
        # a same-track position that disagrees with the extrapolation by
        # over a second is a real jump (a seek somewhere else in Spotify):
        pos = self._position_us_locked()
        jumped = (new_uri and new_uri == old_uri
                  and self._seen_pos_us >= 0
                  and abs(pos - self._seen_pos_us) > POSITION_JUMP_US)
        if new_uri != old_uri and new_status != "Stopped":
            jumped = True          # track change: clients re-anchor via Seeked
        self._status, self._uri, self._seen_pos_us = new_status, new_uri, pos
        if changed:
            emitters.append(lambda: self._emit_properties(IFACE_PLAYER,
                                                          changed))
        if jumped:
            emitters.append(lambda: self._emit_seeked(pos))
        return emitters

    # -- D-Bus plumbing ---------------------------------------------------------
    def _send(self, msg) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.send(msg)
        except Exception as e:
            self._note("send", f"MPRIS signal failed: {e}")

    def _emit_properties(self, iface: str, changed: dict,
                         invalidated: list | None = None) -> None:
        if self._conn is None:
            return
        from jeepney import DBusAddress, new_signal
        addr = DBusAddress(MPRIS_PATH, bus_name=MPRIS_SERVICE,
                           interface=IFACE_PROPS)
        self._send(new_signal(addr, "PropertiesChanged", "sa{sv}as",
                              (iface, changed, invalidated or [])))

    def _emit_seeked(self, position_us: int) -> None:
        if self._conn is None:
            return
        from jeepney import DBusAddress, new_signal
        addr = DBusAddress(MPRIS_PATH, bus_name=MPRIS_SERVICE,
                           interface=IFACE_PLAYER)
        self._send(new_signal(addr, "Seeked", "x", (int(position_us),)))

    # -- volume (Soloist WS when we have one, else 1.0) ------------------------
    @property
    def volume(self) -> float:
        with self._lock:
            return self._volume

    def _refresh_volume(self) -> None:
        """Read Soloist's volume over the WS off-thread (a blocking call can
        take up to its timeout; the bus loop must not wait for it)."""
        ws = self._ws
        if ws is None or not self._owned:
            return
        with self._lock:
            now = self._clock()
            if now < self._vol_next:
                return
            self._vol_next = now + VOLUME_REFRESH_S
        try:
            threading.Thread(target=self._read_volume, daemon=True,
                             name="riko-mpris-vol").start()
        except OSError:
            pass

    def _read_volume(self) -> None:
        try:
            st = self._ws.playback_state() if self._ws else None
            vol = float((st or {}).get("volume")) / 100.0
        except (TypeError, ValueError, AttributeError):
            return
        except Exception:
            return
        if not 0.0 <= vol <= 1.0:
            return
        changed = False
        with self._lock:
            if abs(vol - self._volume) > 1e-3:
                self._volume = vol
                changed = True
        if changed:
            self._emit_properties(IFACE_PLAYER, {"Volume": ("d", vol)})

    def _set_volume(self, value) -> None:
        """Set(Volume): drive Soloist's own volume when we can; otherwise
        the write is ignored (spec-legal: the property still reads back)."""
        try:
            vol = min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return
        ws = self._ws
        if ws is None:
            return

        def go():
            try:
                ok = ws.set_volume(int(round(vol * 100)))
            except Exception:
                return
            if not ok:
                return
            changed = False
            with self._lock:
                if abs(vol - self._volume) > 1e-3:
                    self._volume = vol
                    changed = True
            if changed:
                self._emit_properties(IFACE_PLAYER, {"Volume": ("d", vol)})
        try:
            threading.Thread(target=go, daemon=True,
                             name="riko-mpris-volset").start()
        except OSError:
            pass

    # -- transport (hub control off the bus thread) -----------------------------
    def _transport(self, action: str, value=None, on_ok=None) -> None:
        def go():
            try:
                control = self._control
                if control is None:
                    from riko import hub_client
                    reply = hub_client.get().spotify_control(action,
                                                             value=value)
                else:
                    reply = control(action, value)
                if isinstance(reply, dict) and reply.get("ok") is False:
                    raise RuntimeError(str(reply.get("error") or "rejected"))
            except Exception as e:
                self._note("control", f"MPRIS {action} failed: {e}")
                return
            if on_ok is not None:
                try:
                    on_ok()
                except Exception:
                    pass

        try:
            threading.Thread(target=go, daemon=True,
                             name="riko-mpris-call").start()
        except OSError:
            pass

    # -- the bus thread -----------------------------------------------------------
    def _loop(self) -> None:
        from jeepney.low_level import HeaderFields, MessageType
        conn = self._conn
        while not self._stop.is_set():
            try:
                msg = conn.receive(timeout=RECEIVE_TICK_S)
            except TimeoutError:
                continue
            except Exception:
                break                      # socket died: stop quietly
            if msg.header.message_type != MessageType.method_call:
                continue
            path = msg.header.fields.get(HeaderFields.path, "")
            iface = msg.header.fields.get(HeaderFields.interface, "")
            member = msg.header.fields.get(HeaderFields.member, "")
            sender = msg.header.fields.get(HeaderFields.sender, "")
            try:
                self._handle(conn, msg, path, iface, member, sender)
            except Exception as e:
                self._note("dispatch", f"MPRIS {iface}.{member} failed: {e}")
                try:
                    self._error(conn, msg, "org.freedesktop.DBus."
                                           "Error.Failed", str(e))
                except Exception:
                    pass

    def _reply(self, conn, msg, signature=None, body=()):
        from jeepney import new_method_return
        conn.send(new_method_return(msg, signature, tuple(body)))

    def _error(self, conn, msg, name, text):
        from jeepney import new_error
        conn.send(new_error(msg, name, "s", (text,)))

    def _handle(self, conn, msg, path, iface, member, sender):
        from jeepney.low_level import HeaderFields
        body = tuple(msg.body or ())

        # ---- org.freedesktop.DBus (Peer / Introspectable) ----
        if iface == "org.freedesktop.DBus.Peer":
            if member == "Ping":
                return self._reply(conn, msg)
            if member == "GetMachineId":
                return self._reply(conn, msg, "s", (self._machine_id(),))
            return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                          "UnknownMethod", f"no {member}")
        if iface == "org.freedesktop.DBus.Introspectable":
            if member != "Introspect":
                return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                              "UnknownMethod", f"no {member}")
            return self._reply(conn, msg, "s", (self._introspect(path),))

        # everything below lives on the MPRIS object path
        if path != MPRIS_PATH:
            return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                          "UnknownObject",
                               f"no object {path}")
        if iface == IFACE_PROPS:
            return self._properties(conn, msg, member, body)
        if iface == IFACE_ROOT:
            return self._root_method(conn, msg, member, body)
        if iface == IFACE_PLAYER:
            return self._player_method(conn, msg, member, body)
        return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                      "UnknownInterface",
                           f"no interface {iface}")

    @staticmethod
    def _machine_id() -> str:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                return Path(p).read_text().strip() or "0" * 32
            except OSError:
                continue
        return "0" * 32

    @staticmethod
    def _introspect(path: str) -> str:
        if path == MPRIS_PATH:
            return INTROSPECT_XML
        # walk-the-tree requests from introspection clients
        if path in ("", "/"):
            return "<node><node name=\"org\"/></node>"
        if path == "/org":
            return "<node><node name=\"mpris\"/></node>"
        if path == "/org/mpris":
            return "<node><node name=\"MediaPlayer2\"/></node>"
        return "<node/>"

    # -- property values ------------------------------------------------------------
    def _prop(self, iface: str, name: str):
        """One property as its (signature, value) variant member; raises
        KeyError when unknown."""
        with self._lock:
            status, meta = self._effective_locked(self._snap)
            pos = self._position_us_locked()
        if iface == IFACE_ROOT:
            if name == "Identity":
                return ("s", IDENTITY)
            if name == "DesktopEntry":
                return ("s", desktop_entry())
            if name == "CanQuit":
                return ("b", False)
            if name == "CanRaise":
                return ("b", False)
            if name == "HasTrackList":
                return ("b", False)
            if name == "Fullscreen":
                return ("b", False)
            if name == "CanSetFullscreen":
                return ("b", False)
            if name == "SupportedUriSchemes":
                return ("as", [])
            if name == "SupportedMimeTypes":
                return ("as", [])
            raise KeyError(name)
        if iface == IFACE_PLAYER:
            if name == "PlaybackStatus":
                return ("s", status)
            if name == "Metadata":
                return ("a{sv}", meta)
            if name == "Position":
                return ("x", pos)
            if name == "Volume":
                return ("d", self._volume)
            if name == "Rate":
                return ("d", 1.0)
            if name in ("MinimumRate", "MaximumRate"):
                return ("d", 1.0)
            if name == "LoopStatus":
                return ("s", "None")
            if name == "Shuffle":
                return ("b", False)
            if name in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause",
                        "CanControl"):
                return ("b", True)
            if name == "CanSeek":
                return ("b", True)      # hub control() has a seek action
            raise KeyError(name)
        raise KeyError(f"{iface}:{name}")

    _WRITABLE = {"Volume", "Rate", "LoopStatus", "Shuffle"}

    def _properties(self, conn, msg, member, body):
        if not body:
            return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                          "InvalidArgs", "missing interface")
        iface = str(body[0])
        try:
            if member == "Get":
                val = self._prop(iface, str(body[1]))
                return self._reply(conn, msg, "v", (val,))
            if member == "GetAll":
                names = {
                    IFACE_ROOT: ("Identity", "DesktopEntry", "CanQuit",
                                 "CanRaise", "HasTrackList", "Fullscreen",
                                 "CanSetFullscreen", "SupportedUriSchemes",
                                 "SupportedMimeTypes"),
                    IFACE_PLAYER: ("PlaybackStatus", "LoopStatus", "Rate",
                                   "Shuffle", "Metadata", "Volume",
                                   "Position", "MinimumRate", "MaximumRate",
                                   "CanGoNext", "CanGoPrevious", "CanPlay",
                                   "CanPause", "CanSeek", "CanControl"),
                }.get(iface)
                if names is None:
                    return self._error(conn, msg,
                                       "org.freedesktop.DBus.Error."
                                       "UnknownInterface",
                                       f"no interface {iface}")
                out = {}
                for n in names:
                    try:
                        out[n] = self._prop(iface, n)
                    except KeyError:
                        continue
                return self._reply(conn, msg, "a{sv}", (out,))
            if member == "Set":
                iface, name, value = str(body[0]), str(body[1]), body[2]
                if name not in self._WRITABLE:
                    try:
                        self._prop(iface, name)      # unknown vs read-only
                    except KeyError:
                        return self._error(
                            conn, msg, "org.freedesktop.DBus.Error."
                                       "UnknownProperty", f"no {name}")
                    return self._error(conn, msg, "org.freedesktop.DBus."
                                                  "Error.PropertyReadOnly",
                                       f"{name} is read-only")
                raw = value[1] if isinstance(value, tuple) and \
                        len(value) == 2 else value
                if name == "Volume":
                    self._set_volume(raw)
                # Rate/LoopStatus/Shuffle: accept and ignore — she plays at
                # 1x in whatever order Spotify's device has; nothing on the
                # hub exposes per-player rate.
                return self._reply(conn, msg)
        except KeyError:
            return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                          "UnknownProperty",
                               f"no property on {iface}")
        return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                      "UnknownMethod", f"no {member}")

    # -- root / player methods ----------------------------------------------------
    def _root_method(self, conn, msg, member, body):
        if member == "Fullscreen":
            want = bool(body[0]) if body else False
            return self._reply(conn, msg, "b", (want,))
        if member in ("Raise", "Quit"):
            return self._reply(conn, msg)      # CanRaise/CanQuit are false:
                                               # accept the call, do nothing
        with self._lock:
            if member == "CanQuit":
                return self._reply(conn, msg, "b", (False,))
            if member == "CanRaise":
                return self._reply(conn, msg, "b", (False,))
            if member == "HasTrackList":
                return self._reply(conn, msg, "b", (False,))
        return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                      "UnknownMethod", f"no {member}")

    def _player_method(self, conn, msg, member, body):
        simple = {"Play": "play", "Pause": "pause", "PlayPause": "toggle",
                  "Next": "next", "Previous": "previous",
                  "Stop": "pause"}    # hub has no stop: pause is the bar's
                                      # expectation (docs/SOLOIST.md)
        if member in simple:
            self._transport(simple[member])
            return self._reply(conn, msg)
        if member in ("Seek", "SetPosition"):
            with self._lock:
                cur = self._position_us_locked()
            if member == "Seek":
                try:
                    offset = int(body[0])
                except (IndexError, TypeError, ValueError):
                    offset = 0
                target_us = max(0, cur + offset)
            else:
                try:
                    target_us = max(0, int(body[1]))
                except (IndexError, TypeError, ValueError):
                    target_us = 0

            def on_ok():
                with self._lock:
                    self._seen_pos_us = target_us
                self._emit_seeked(target_us)

            # hub 'seek' takes SECONDS (docs/API.md)
            self._transport("seek", target_us / 1_000_000.0, on_ok=on_ok)
            return self._reply(conn, msg)
        if member == "OpenUri":
            return self._error(conn, msg, "org.mpris.MediaPlayer2."
                                          "NotSupported",
                               "Riko plays Spotify, not URIs "
                               "(SupportedUriSchemes is empty)")
        return self._error(conn, msg, "org.freedesktop.DBus.Error."
                                      "UnknownMethod", f"no {member}")
