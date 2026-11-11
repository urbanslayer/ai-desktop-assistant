"""MPRIS export tests (riko/music/mpris.py).

Pure logic tests run anywhere; the bus tests spin a throwaway PRIVATE
dbus-daemon (never the owner's session bus) and skip cleanly when
dbus-daemon/gdbus/jeepney are missing — same "detect, never install" habit
as the module itself.
"""
import os
import shutil
import signal
import subprocess
import threading
import time

import pytest

from riko.music import mpris
from riko.music.mpris import (MPRIS_PATH, MPRIS_SERVICE, IFACE_PLAYER,
                              IFACE_PROPS,
                              RikoMprisPublisher, desktop_entry, metadata,
                              playback_status, qualifies, resolve_mode,
                              should_publish, track_url, trackid_path)

jeepney = pytest.importorskip("jeepney")
HAVE_DBUS = bool(shutil.which("dbus-daemon"))
bus_only = pytest.mark.skipif(not HAVE_DBUS, reason="no dbus-daemon on this box")


def snap(**kw):
    s = {"playing": True, "uri": "spotify:track:3nDuOwLMWTGJPR60mGZLtB",
         "track": "Test Song", "artists": "Aria, Beta", "album": "Album",
         "duration_ms": 210500, "position_ms": 4200, "device": "Riko",
         "art_url": "https://i.scdn.co/image/abcd", "context_uri": ""}
    s.update(kw)
    return s


# ── pure config/gating ────────────────────────────────────────────────────────

def test_resolve_mode():
    assert resolve_mode({}) == "riko"                  # default
    assert resolve_mode(None) == "riko"
    assert resolve_mode({"mpris": "any"}) == "any"
    assert resolve_mode({"mpris": " OFF "}) == "off"
    assert resolve_mode({"mpris": "yes-please"}) == "off"   # fail closed


def test_should_publish_modes_and_players():
    assert should_publish("riko", "soloist")
    assert should_publish("any", "external")
    assert not should_publish("off", "soloist")
    # legacy spotifyd ships its own MPRIS name: never double up
    assert not should_publish("riko", "spotifyd")
    assert not should_publish("any", "spotifyd")
    assert not should_publish("riko", "off")


def test_desktop_entry_only_when_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "sys"))
    assert desktop_entry() == ""
    app = tmp_path / "sys" / "applications"
    app.mkdir(parents=True)
    (app / "riko.desktop").write_text("[Desktop Entry]\nName=Riko\n")
    assert desktop_entry() == "riko"


def test_art_survives_ws_beats():
    # the WS fast path sends art_url="": it must never blank the artwork
    # the hub snapshot carried for the SAME track (bar cover art)
    p = RikoMprisPublisher(mode="riko", device_name="Riko",
                           log=lambda line: None)
    p.update(snap(art_url="https://i.scdn.co/image/abcd"))
    p.update(snap(art_url=""))                       # WS beat, same uri
    assert metadata(p._snap)["mpris:artUrl"] == (
        "s", "https://i.scdn.co/image/abcd")
    p.update(snap(uri="spotify:track:NEW1", art_url=""))   # new track: reset
    assert "mpris:artUrl" not in metadata(p._snap)


def test_trackid_and_url():
    # leading digits are legal in the URI but not in object-path elements:
    # the export prefixes one 't' (verified shape from the smoke dump)
    path = trackid_path("spotify:track:3nDuOwLMWTGJPR60mGZLtB")
    assert path == MPRIS_PATH + "/TrackList/Track/t3nDuOwLMWTGJPR60mGZLtB"
    # object paths: no leading digit, no junk
    p2 = trackid_path("spotify:track:1bad-id/x")
    assert p2.rsplit("/", 1)[-1][0].isalpha() and " " not in p2
    assert trackid_path("").endswith("NoTrack")
    assert track_url("spotify:track:XYZ") == "https://open.spotify.com/track/XYZ"
    assert track_url("spotify:episode:XYZ") == ""


def test_playback_status():
    assert playback_status(snap()) == "Playing"
    assert playback_status(snap(playing=False)) == "Paused"
    assert playback_status(snap(playing=False, uri="")) == "Stopped"
    assert playback_status({}) == "Stopped"


def test_qualifies_device_gate():
    assert qualifies(snap(), "riko", "Riko")
    assert qualifies(snap(device="riko "), "riko", "Riko")   # sloppy match
    assert not qualifies(snap(device="Phone"), "riko", "Riko")
    assert qualifies(snap(device="Phone"), "any", "Riko")
    assert not qualifies(snap(), "off", "Riko")
    assert not qualifies({"playing": False, "uri": ""}, "any", "Riko")


def test_metadata_variant_types_and_filtering():
    meta = metadata(snap())
    assert meta["mpris:length"] == ("x", 210500000)          # int64 microseconds
    assert meta["mpris:trackid"][0] == "o"                    # object path
    assert meta["xesam:artist"] == ("as", ["Aria", "Beta"])
    assert meta["xesam:title"] == ("s", "Test Song")
    assert meta["mpris:artUrl"] == ("s", "https://i.scdn.co/image/abcd")
    assert meta["xesam:url"][1].startswith("https://open.spotify.com/track/")
    # http art never leaks; missing fields simply absent; no uri -> {}
    assert "mpris:artUrl" not in metadata(snap(art_url="http://x/y.jpg"))
    assert "xesam:album" not in metadata(snap(album=""))
    assert metadata({"uri": "", "playing": True}) == {}


# ── state machine (no bus needed) ─────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def _signals(rec, member):
    from jeepney.low_level import HeaderFields
    out = []
    for m in rec.sent:
        f = m.header.fields
        if str(f.get(HeaderFields.member, "")) == member:
            out.append(tuple(m.body or ()))
    return out


def test_position_extrapolation_and_clamp():
    t = [100.0]
    p = RikoMprisPublisher(mode="riko", clock=lambda: t[0])
    p.update(snap(position_ms=5000))
    assert p._position_us_locked() == 5_000_000
    t[0] += 2.0                                  # 2 s of playback elapsed
    assert p._position_us_locked() == 7_000_000
    t[0] += 300.0                                # clamped at the track end
    assert p._position_us_locked() == 210_500_000
    p.update(snap(playing=False))                # paused: frozen, no drift
    t[0] += 60.0
    assert p._position_us_locked() == 4_200_000


def test_update_emits_properties_changed_and_seeked():
    p = RikoMprisPublisher(mode="riko")
    p.update(snap())                             # seed (no conn yet: no-op)
    rec = Recorder()
    p._conn = rec
    p.update(snap(playing=False))                # Playing -> Paused
    pc = _signals(rec, "PropertiesChanged")
    assert len(pc) == 1
    assert pc[0][0] == IFACE_PLAYER
    assert pc[0][1]["PlaybackStatus"] == ("s", "Paused")
    assert pc[0][1]["Metadata"][1]["xesam:title"] == ("s", "Test Song")
    p.update(snap(playing=False))                # identical beat: silence
    assert len(_signals(rec, "PropertiesChanged")) == 1
    p.update(snap(uri="spotify:track:NEW1"))     # track change: Seeked too
    assert len(_signals(rec, "PropertiesChanged")) == 2
    assert len(_signals(rec, "Seeked")) == 1
    assert _signals(rec, "Seeked")[0][0] > 0


def test_update_detects_same_track_jump():
    t = [0.0]
    p = RikoMprisPublisher(mode="riko", clock=lambda: t[0])
    p.update(snap(position_ms=10000))
    rec = Recorder()
    p._conn = rec
    p.update(snap(position_ms=90000))            # +80 s vs extrapolation
    assert len(_signals(rec, "Seeked")) == 1
    p.update(snap(position_ms=90500))            # tiny drift: no Seeked
    assert len(_signals(rec, "Seeked")) == 1


def test_nonqualifying_state_reports_stopped():
    p = RikoMprisPublisher(mode="riko", device_name="Riko")
    p.update(snap(device="Phone"))
    status, meta = p._effective_locked(p._snap)
    assert (status, meta) == ("Stopped", {})
    p2 = RikoMprisPublisher(mode="any", device_name="Riko")
    p2.update(snap(device="Phone"))
    assert p2._effective_locked(p2._snap)[0] == "Playing"


class FakeWs:
    def __init__(self, volume=70):
        self.volume = volume
        self.set_calls = []

    def available(self):
        return True

    def playback_state(self):
        return {"volume": self.volume, "status": "playing"}

    def set_volume(self, percent):
        self.set_calls.append(percent)
        self.volume = percent
        return True


def test_volume_read_and_write_via_ws():
    ws = FakeWs(volume=40)
    p = RikoMprisPublisher(mode="riko", ws=ws, connector=lambda bus=None: None)
    p._owned = True                              # _refresh_volume gate
    p._read_volume()                             # direct: no thread race
    assert p.volume == pytest.approx(0.4)
    p._set_volume(0.55)                          # worker thread inside
    for _ in range(100):
        if ws.set_calls:
            break
        time.sleep(0.02)
    assert ws.set_calls == [55]
    p._set_volume("nonsense")                    # ignored, no crash
    p_no = RikoMprisPublisher(mode="riko", ws=None)
    p_no._set_volume(0.2)                        # no ws: accept & ignore


def test_start_without_bus_is_silent_false(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    p = RikoMprisPublisher(mode="riko", log=lambda line: None)
    assert p.start() is False                    # logged, never raised
    p.stop()                                     # idempotent


def test_transport_failures_log_once_not_raise():
    boom = lambda a, v=None: (_ for _ in ()).throw(RuntimeError("hub down"))
    p = RikoMprisPublisher(mode="riko", control=boom, log=lambda line: None)
    p._transport("next")                         # worker thread
    for _ in range(100):
        if p._noted:
            break
        time.sleep(0.02)
    assert "control" in p._noted


# ── duck exclusion (MprisDuck must never duck our own name) ─────────────────

def test_duck_skips_our_own_mpris_name():
    from riko.music.spotifyd import MprisDuck

    class Props:
        def __init__(self):
            self.volume, self.sets = 0.8, []

        def Get(self, iface, key):
            return self.volume

        def Set(self, iface, key, value):
            self.sets.append(float(value))
            self.volume = float(value)

    ours, theirs = Props(), Props()
    duck = MprisDuck(0.15, bus={mpris.MPRIS_SERVICE.split(".")[-1]: ours,
                                "spotifyd": theirs})
    duck.duck()
    assert ours.sets == [] and theirs.sets          # only the foreign player
    duck.restore()
    assert ours.volume == 0.8


# ── hub snapshot art_url (the hub side of the contract) ──────────────────────

def test_hub_snapshot_picks_largest_https_art():
    from hub.spotify import track_snapshot
    cp = {"is_playing": True, "progress_ms": 1000,
          "device": {"name": "Riko"},
          "item": {"name": "T", "uri": "spotify:track:X", "duration_ms": 10,
                   "artists": [{"name": "A"}],
                   "album": {"name": "Al", "images": [
                       {"url": "https://e/medium.jpg", "width": 300,
                        "height": 300},
                       {"url": "http://e/large.jpg", "width": 640,
                        "height": 640},                 # http: filtered out
                       {"url": "https://e/large.jpg", "width": 640,
                        "height": 640}]}}}
    snapd = track_snapshot(cp)
    assert snapd["art_url"] == "https://e/large.jpg"
    assert track_snapshot(None)["art_url"] == ""
    bare = dict(cp, item={**cp["item"], "album": {}})
    assert track_snapshot(bare)["art_url"] == ""


def test_soloist_local_snapshot_has_empty_art_url():
    from riko.music.soloist import soloist_snapshot
    assert soloist_snapshot({"status": "idle"})["art_url"] == ""


# ── tray checkbox ─────────────────────────────────────────────────────────────

def test_music_menu_mpris_checkbox(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path / "state"))
    from PySide6.QtWidgets import QMenu
    import desktop

    class MenuPlayer:
        kind, mode = "soloist", "managed"

        def status(self):
            return {"mode": "managed", "running": True, "kind": "soloist"}

    class StubMpris:
        running = True

        def __init__(self):
            self.sets = []

        def set_enabled(self, on):
            self.sets.append(on)
            self.running = on

    stub = StubMpris()
    parent = QMenu()                              # keep a ref (shiboken)
    menu = desktop.build_music_menu(parent, MenuPlayer(),
                                    notify=lambda *a: None, mpris=stub)
    acts = {a.text(): a for a in menu.actions()}
    box = next(a for t, a in acts.items() if "MPRIS" in t)
    assert box.isChecked()
    box.toggle()
    assert stub.sets == [False]
    from riko import appstate
    assert appstate.load_music_mpris() is False   # persisted like the ticker


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


# ── the real thing on a PRIVATE bus ───────────────────────────────────────────

@pytest.fixture(scope="module")
def private_bus():
    """Throwaway session bus (address + pid); torn down after the module."""
    if not HAVE_DBUS:
        pytest.skip("no dbus-daemon")
    proc = subprocess.run(
        ["dbus-daemon", "--session", "--print-address", "--print-pid",
         "--fork"], capture_output=True, text=True, check=True)
    lines = proc.stdout.split("\n")
    addr, pid = lines[0].strip(), int(lines[1].strip())
    yield addr, pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


@pytest.fixture
def bus_env(private_bus, monkeypatch):
    addr, _pid = private_bus
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", addr)
    return addr


@pytest.fixture
def pub(bus_env):
    calls = []
    p = RikoMprisPublisher(mode="riko", device_name="Riko",
                           control=lambda a, v=None: calls.append((a, v)),
                           ws=None, log=print)
    p.test_calls = calls
    yield p
    p.stop()


def _poll(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


class BusError(Exception):
    """A D-Bus error reply, with its error name."""

    def __init__(self, name, body=()):
        super().__init__(name or str(body))
        self.name = name
        self.body = body


class Caller:
    """Raw method calls on the private bus via jeepney. (Not gdbus:
    tests/conftest.py guards subprocesses against compositor/session-bus
    tools wholesale — a bus-name argument would not tell private from
    live, so nothing shell-out survives; the gdbus-style verification dump
    runs outside pytest in the scratch smoke script.)"""

    def __init__(self):
        from jeepney.io.threading import open_dbus_connection
        self.conn = open_dbus_connection(bus="SESSION")
        self._serial = 1000

    def call(self, dest, path, iface, member, signature=None, body=(),
             timeout=10.0):
        from jeepney import DBusAddress, new_method_call
        from jeepney.low_level import HeaderFields, MessageType
        self._serial += 1
        serial = self._serial
        msg = new_method_call(DBusAddress(path, bus_name=dest,
                                          interface=iface),
                              member, signature, tuple(body))
        self.conn.send(msg, serial=serial)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = self.conn.receive(timeout=0.5)
            except TimeoutError:
                continue
            hdr = r.header
            if hdr.fields.get(HeaderFields.reply_serial) != serial:
                continue
            if hdr.message_type == MessageType.method_return:
                return tuple(r.body or ())
            raise BusError(
                str(hdr.fields.get(HeaderFields.error_name, "")),
                tuple(r.body or ()))
        raise TimeoutError(f"{iface}.{member} never answered")

    def props_get(self, iface, name):
        value, = self.call(MPRIS_SERVICE, MPRIS_PATH, IFACE_PROPS, "Get",
                           "ss", (iface, name))
        return value

    def props_getall(self, iface):
        values, = self.call(MPRIS_SERVICE, MPRIS_PATH, IFACE_PROPS,
                            "GetAll", "s", (iface,))
        return dict(values)

    def player(self, member, signature=None, body=()):
        return self.call(MPRIS_SERVICE, MPRIS_PATH, IFACE_PLAYER, member,
                         signature, body)

    def list_names(self):
        names, = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus", "ListNames")
        return list(names)

    def close(self):
        self.conn.close()


class BusClient:
    """Signal observer on the same private bus (match rule installed)."""

    def __init__(self):
        from jeepney.bus_messages import message_bus
        from jeepney.io.threading import open_dbus_connection
        self.conn = open_dbus_connection(bus="SESSION")
        self.conn.send(message_bus.AddMatch("type=\'signal\'"), serial=0x99)
        self.seen, self._stop = [], threading.Event()

        def pump():
            from jeepney.low_level import HeaderFields, MessageType
            while not self._stop.is_set():
                try:
                    m = self.conn.receive(timeout=0.2)
                except TimeoutError:
                    continue
                except Exception:
                    break
                hdr = m.header
                if hdr.message_type in (MessageType.method_return,
                                        MessageType.error):
                    continue
                f = hdr.fields
                self.seen.append((str(f.get(HeaderFields.interface, "")),
                                  str(f.get(HeaderFields.member, "")),
                                  tuple(m.body or ())))
        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()

    def wait(self, iface, member, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for got in self.seen:
                if got[0] == iface and got[1] == member:
                    return got
            time.sleep(0.05)
        return None

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.conn.close()


@pytest.fixture
def caller(bus_env):
    c = Caller()
    yield c
    c.close()


@bus_only
def test_bus_name_owned_and_introspectable(pub, caller):
    pub.update(snap())
    assert pub.start()
    assert MPRIS_SERVICE in caller.list_names()
    (xml,) = caller.call(MPRIS_SERVICE, MPRIS_PATH,
                         "org.freedesktop.DBus.Introspectable", "Introspect")
    for needle in ('name="org.mpris.MediaPlayer2"',
                   'name="org.mpris.MediaPlayer2.Player"',
                   'name="Seeked"', 'name="Metadata"'):
        assert needle in xml, needle
    # second instance must decline, not crash
    p2 = RikoMprisPublisher(mode="riko", log=lambda line: None)
    assert p2.start() is False
    p2.stop()


@bus_only
def test_bus_properties_match_playing_snapshot(pub, caller):
    pub.update(snap())
    assert pub.start()
    pl = caller.props_getall(IFACE_PLAYER)
    assert pl["PlaybackStatus"] == ("s", "Playing")
    assert pl["Position"][0] == "x"                  # spec: int64
    meta = pl["Metadata"][1]
    assert meta["mpris:length"] == ("x", 210500000)  # int64 microseconds
    assert meta["mpris:trackid"][0] == "o"           # object path
    assert meta["xesam:artist"] == ("as", ["Aria", "Beta"])
    assert meta["xesam:title"] == ("s", "Test Song")
    assert meta["mpris:artUrl"] == ("s", "https://i.scdn.co/image/abcd")
    assert pl["CanControl"] == ("b", True)
    assert pl["CanSeek"] == ("b", True)              # hub has a seek action
    assert pl["CanGoNext"] == pl["CanGoPrevious"] == ("b", True)
    root = caller.props_getall("org.mpris.MediaPlayer2")
    assert root["Identity"] == ("s", "Riko")
    assert root["CanQuit"] == ("b", False)
    assert root["CanRaise"] == ("b", False)
    assert root["HasTrackList"] == ("b", False)
    assert root["SupportedUriSchemes"] == ("as", [])
    assert root["SupportedMimeTypes"] == ("as", [])


@bus_only
def test_bus_device_gate_and_pause(pub, caller):
    pub.update(snap(device="Pixel Phone"))           # someone else's playback
    assert pub.start()
    assert caller.props_get(IFACE_PLAYER, "PlaybackStatus") == ("s", "Stopped")
    assert caller.props_get(IFACE_PLAYER, "Metadata") == ("a{sv}", {})
    pub.update(snap(device="Riko", playing=False))   # our player, paused
    assert _poll(lambda: caller.props_get(IFACE_PLAYER,
                                          "PlaybackStatus") == ("s", "Paused"))


@bus_only
def test_bus_signals(pub, caller):
    pub.update(snap())
    assert pub.start()
    client = BusClient()
    try:
        pub.update(snap(playing=False))
        got = client.wait("org.freedesktop.DBus.Properties",
                          "PropertiesChanged")
        assert got is not None, "PropertiesChanged never arrived"
        assert got[2][0] == IFACE_PLAYER
        assert got[2][1]["PlaybackStatus"] == ("s", "Paused")
        pub.update(snap(uri="spotify:track:NEXT1"))
        got = client.wait(IFACE_PLAYER, "Seeked")
        assert got is not None, "Seeked never arrived"
        assert isinstance(got[2][0], int) and got[2][0] >= 0
    finally:
        client.close()


@bus_only
def test_bus_transport_calls_hit_the_hub(pub, caller):
    pub.update(snap())
    assert pub.start()
    calls = pub.test_calls

    def call(member, signature=None, body=()):
        caller.player(member, signature, body)       # returns fast: the hub
    # round trip happens on a worker thread, never blocking the bus

    call("PlayPause")
    assert _poll(lambda: ("toggle", None) in calls)
    call("Next")
    assert _poll(lambda: ("next", None) in calls)
    call("Previous")
    assert _poll(lambda: ("previous", None) in calls)
    call("Play")
    assert _poll(lambda: ("play", None) in calls)
    call("Pause")
    assert _poll(lambda: calls.count(("pause", None)) == 1)
    call("Stop")                                     # no hub stop -> pause
    assert _poll(lambda: calls.count(("pause", None)) == 2)
    n = len(calls)
    call("Seek", "x", (30_000_000,))                 # +30 s RELATIVE
    assert _poll(lambda: len(calls) > n)
    assert calls[-1][0] == "seek"
    assert calls[-1][1] > 30.0                       # offset ADDED to position
    n = len(calls)
    call("SetPosition", "ox",
         (MPRIS_PATH + "/TrackList/Track/x", 12_500_000))   # absolute
    assert _poll(lambda: len(calls) > n)
    assert calls[-1] == ("seek", 12.5)               # hub takes SECONDS
    # OpenUri with empty SupportedUriSchemes -> NotSupported error
    try:
        call("OpenUri", "s", ("file:///x",))
        raise AssertionError("OpenUri should error")
    except BusError as e:
        assert e.name == "org.mpris.MediaPlayer2.NotSupported"


@bus_only
def test_bus_volume_roundtrip_via_ws(pub, caller):
    ws = FakeWs(volume=40)
    pub._ws = ws
    pub.update(snap())
    assert pub.start()
    assert _poll(lambda: pub.volume == pytest.approx(0.4))
    assert _poll(lambda: caller.props_get(IFACE_PLAYER, "Volume")
                 == ("d", 0.4))
    caller.call(MPRIS_SERVICE, MPRIS_PATH, IFACE_PROPS, "Set", "ssv",
                (IFACE_PLAYER, "Volume", ("d", 0.65)))
    assert _poll(lambda: ws.set_calls == [65])       # Soloist WS percent
    assert _poll(lambda: caller.props_get(IFACE_PLAYER, "Volume")
                 == ("d", 0.65))
    with pytest.raises(BusError) as exc:             # read-only property
        caller.call(MPRIS_SERVICE, MPRIS_PATH, IFACE_PROPS, "Set", "ssv",
                    (IFACE_PLAYER, "PlaybackStatus", ("s", "Playing")))
    assert exc.value.name.endswith("PropertyReadOnly")


@bus_only
def test_bus_mode_any_mirrors_foreign_device(bus_env):
    calls = []
    p = RikoMprisPublisher(mode="any", device_name="Riko",
                           control=lambda a, v=None: calls.append((a, v)),
                           log=print)
    p.update(snap(device="Pixel Phone"))
    try:
        assert p.start()
        c = Caller()
        try:
            assert c.props_get(IFACE_PLAYER,
                               "PlaybackStatus") == ("s", "Playing")
        finally:
            c.close()
    finally:
        p.stop()


@bus_only
def test_bus_stop_releases_the_name(pub, bus_env):
    pub.update(snap())
    assert pub.start()
    pub.stop()
    probe = Caller()
    try:
        assert _poll(lambda: MPRIS_SERVICE not in probe.list_names())
    finally:
        probe.close()
    assert pub.start()                               # restart reclaims it


# (the /api/spotify/control seek route itself is covered end-to-end against
# the fake Spotify in tests/test_hub_spotify.py)
