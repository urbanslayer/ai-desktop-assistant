"""Music phase 1 (desktop side): the spotifyd child-process manager, the
MPRIS duck, and the loopback OAuth listener. Fakes only — fake popen/PATH
shim for the subprocess (NEVER a real spotifyd), a fake props-bus for
ducking, a fake hub client for the listener. No compositor, no live session,
no real network, and a fake HOME so the manager's real file writes
(~/.config/riko/spotifyd.conf, ~/.cache/riko/spotifyd) never touch the
owner's home dir."""
import io
import os
import pathlib
import re
import urllib.request

import pytest

from riko.music import spotifyd as sd
from riko.music.oauth_local import OAuthListener
from riko.music.spotifyd import MprisDuck, SETUP_TEXT, SpotifydPlayer


# ── spotifyd manager ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """Fake HOME with a logged-in Riko cache so spawn tests pass the
    credentials gate; per-test code deletes/moves files as needed."""
    home = tmp_path / "home"
    creds = home / ".cache" / "riko" / "spotifyd" / "oauth" / "credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text('{"preset": true}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("RIKO_CONFIG_DIR", raising=False)
    monkeypatch.delenv("RIKO_SPOTIFYD_CACHE", raising=False)
    return home


class FakeProc:
    def __init__(self, argv=None, returncode=None):
        self.argv = argv
        self.alive = True
        self.terminated = False
        self.pid = 4242
        self.returncode = returncode
        self.stdout = None                           # no pipe to drain

    def poll(self):
        return None if self.alive else (
            self.returncode if self.returncode is not None else 0)

    def terminate(self):
        self.terminated = True
        self.alive = False
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class DyingProc(FakeProc):
    """Exits right away, code 2, spilling `output` on its (piped) stdout."""

    def __init__(self, output="", returncode=2):
        super().__init__(returncode=returncode)
        self.alive = False
        self.stdout = io.StringIO(output)


class RecordingPopen:
    def __init__(self, factory=None):
        self.calls = []
        self.kwargs = []
        self._factory = factory

    def __call__(self, argv, **kw):
        proc = (self._factory(list(argv)) if self._factory
                else FakeProc(list(argv)))
        self.calls.append(proc)
        self.kwargs.append(kw)
        return proc


def managed(**kw):
    kw.setdefault("which", lambda name: "/usr/bin/spotifyd")
    kw.setdefault("popen", RecordingPopen())
    return SpotifydPlayer({"spotifyd": "managed"}, **kw)


def test_missing_binary_gives_setup_text():
    popen = RecordingPopen()
    player = SpotifydPlayer({"spotifyd": "managed"},
                            which=lambda name: None, popen=popen)
    out = player.start()
    assert popen.calls == []                              # never installs, never starts
    assert out["ok"] is False
    assert "sudo pacman -S spotifyd" in out["message"]
    # the setup text carries the exact authenticate command WITH the cache path
    assert "spotifyd authenticate --cache-path ~/.cache/riko/spotifyd" \
        in out["message"]
    assert SETUP_TEXT == player.setup_text()
    assert player.status()["running"] is False
    assert player.status()["setup_needed"] is True
    assert player.status()["detail"] == SETUP_TEXT


def test_off_mode_is_inert():
    popen = RecordingPopen()
    player = SpotifydPlayer({"spotifyd": "off"},
                            which=lambda name: "/usr/bin/spotifyd",
                            popen=popen)
    assert "off" in player.start()["message"]
    assert "off" in player.stop()["message"]
    assert popen.calls == []
    assert player.status()["mode"] == "off"


def test_managed_start_argv_and_stop(fake_home):
    popen = RecordingPopen()
    player = managed(device_name="Riko", popen=popen)
    out = player.start()
    assert out["ok"] and "Riko" in out["message"]
    proc = popen.calls[0]
    assert proc.argv[0] == "/usr/bin/spotifyd"
    assert "--device-name" in proc.argv
    assert proc.argv[proc.argv.index("--device-name") + 1] == "Riko"
    assert "--backend" in proc.argv
    # spotifyd 0.4.2 has no 'pipewire' backend; PipeWire = PulseAudio protocol
    assert proc.argv[proc.argv.index("--backend") + 1] == "pulseaudio"
    # self-contained: Riko's own conf + absolute cache, never the owner's
    conf = proc.argv[proc.argv.index("--config-path") + 1]
    assert conf == str(fake_home / ".config" / "riko" / "spotifyd.conf")
    cache = proc.argv[proc.argv.index("--cache-path") + 1]
    assert cache == str(fake_home / ".cache" / "riko" / "spotifyd")
    assert os.path.isabs(conf) and os.path.isabs(cache)
    # cwd = the cache dir, so a relative path can never land in the repo/home
    assert popen.kwargs[0]["cwd"] == cache
    assert player.status()["running"] and player.status()["pid"] == 4242
    st = player.stop()
    assert st["ok"] and proc.terminated
    assert player.stop()["message"] == "player is not running"
    assert popen.calls == [proc]                          # one spawn only


def test_managed_start_is_idempotent():
    popen = RecordingPopen()
    player = managed(popen=popen)
    assert player.start()["ok"]
    out = player.start()
    assert out["ok"] and "already running" in out["message"]
    assert len(popen.calls) == 1


def test_config_backend_pipewire_is_mapped_to_pulseaudio():
    notes = []
    popen = RecordingPopen()
    player = managed(popen=popen, log=notes.append)
    player2 = SpotifydPlayer({"spotifyd": "managed", "backend": "pipewire"},
                             popen=popen, log=notes.append)
    assert player.backend == "pulseaudio"                 # default already safe
    assert player2.backend == "pulseaudio"
    assert player2.start()["ok"]
    argv = popen.calls[-1].argv
    assert argv[argv.index("--backend") + 1] == "pulseaudio"
    assert any("pipewire" in n and "pulseaudio" in n for n in notes)


def test_written_conf_is_minimal_private_and_hook_free(fake_home):
    player = managed(device_name="mikoto_spotify")
    assert player.start()["ok"]
    conf = fake_home / ".config" / "riko" / "spotifyd.conf"
    text = conf.read_text()
    assert oct(conf.stat().st_mode & 0o777) == "0o600"
    assert 'device_name = "mikoto_spotify"' in text
    assert 'backend = "pulseaudio"' in text
    assert f'cache_path = "{player.cache_dir}"' in text   # ABSOLUTE, not relative
    assert "use_mpris = true" in text
    assert 'device_type = "computer"' in text
    assert "bitrate = 320" in text
    assert "initial_volume" in text
    assert "hook" not in text                             # no on_song_change_hook
    assert "username" not in text                         # never the old account


def test_credentials_discovered_and_copied_not_moved(fake_home):
    # the owner's case: ncspot-era relative cache_path put the login in
    # ~/cache_directory/oauth; Riko copies it, source stays put.
    dest = fake_home / ".cache" / "riko" / "spotifyd" / "oauth" / "credentials.json"
    dest.unlink()
    src = fake_home / "cache_directory" / "oauth" / "credentials.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"owner": "login"}')
    notes = []
    popen = RecordingPopen()
    player = managed(popen=popen, log=notes.append)
    assert player.start()["ok"]
    assert dest.read_text() == '{"owner": "login"}'
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    assert src.is_file()                                  # never moved or deleted
    assert any("copied" in n and str(src) in n for n in notes)
    assert popen.calls and popen.calls[0].argv            # and it spawned


def test_no_credentials_does_not_spawn(fake_home):
    (fake_home / ".cache" / "riko" / "spotifyd" / "oauth"
     / "credentials.json").unlink()
    popen = RecordingPopen()
    player = managed(popen=popen)
    out = player.start()
    assert popen.calls == []                              # no login = no child
    assert out["ok"] is False
    assert ("spotifyd authenticate --cache-path "
            f"{fake_home / '.cache' / 'riko' / 'spotifyd'}") in out["message"]
    assert player.status()["login_needed"] is True


def test_child_that_dies_immediately_is_reported():
    popen = RecordingPopen()
    player = managed(popen=popen)
    player.start()
    popen.calls[0].alive = False                          # crashed behind us
    assert player.status()["running"] is False
    assert player.status()["pid"] is None


def test_early_exit_surfaces_spotifyds_own_error():
    popen = RecordingPopen(factory=lambda argv: DyingProc(
        "info: starting spotifyd v0.4.2\n"
        "error: invalid value 'pipewire' for '--backend <BACKEND>'\n"
        "For more information, try '--help'.\n"))
    player = managed(popen=popen)
    out = player.start()
    assert out["ok"] is False
    assert "code 2" in out["message"]
    assert "invalid value 'pipewire'" in out["message"]   # the REAL reason
    assert "authenticate" not in out["message"]           # no login guess here


def test_early_exit_says_login_only_when_output_says_auth():
    popen = RecordingPopen(factory=lambda argv: DyingProc(
        "error: failed to open credentials file: No such file or directory"))
    player = managed(popen=popen)
    out = player.start()
    assert "credentials file" in out["message"]
    assert "spotifyd authenticate --cache-path" in out["message"]


def test_managed_config_cache_override(fake_home, tmp_path):
    alt = tmp_path / "my-spotifyd-cache"
    popen = RecordingPopen()
    player = SpotifydPlayer({"spotifyd": "managed", "spotifyd_cache": str(alt)},
                            which=lambda name: "/usr/bin/spotifyd", popen=popen)
    out = player.start()
    assert popen.calls == []                              # no login in the alt cache
    assert f"spotifyd authenticate --cache-path {alt}" in out["message"]


def test_external_mode_never_spawns():
    popen = RecordingPopen()
    player = SpotifydPlayer({"spotifyd": "external"},
                            which=lambda name: "/usr/bin/spotifyd",
                            popen=popen)
    out = player.start()
    assert popen.calls == []
    assert out["ok"] is False and "external" in out["message"]
    assert "won't stop it" in player.stop()["message"]


def test_binary_override_path_exists_check(tmp_path):
    missing = tmp_path / "nope"
    player = SpotifydPlayer({"spotifyd": "managed", "binary": str(missing)},
                            which=lambda name: None, popen=RecordingPopen())
    assert "sudo pacman -S spotifyd" in player.start()["message"]
    real = tmp_path / "spotifyd"
    real.write_text("#!/bin/sh\n")
    popen = RecordingPopen()
    player = SpotifydPlayer({"spotifyd": "managed", "binary": str(real)},
                            which=lambda name: None, popen=popen)
    assert player.start()["ok"]
    assert popen.calls[0].argv[0] == str(real)


def test_device_name_defaults_to_riko():
    assert SpotifydPlayer({"spotifyd": "managed"}).device_name == "Riko"
    assert SpotifydPlayer({}, device_name="  ").device_name == "Riko"
    assert SpotifydPlayer({}, device_name="Riko-chan").device_name == "Riko-chan"
    # bad mode strings collapse to off (config typo safety)
    assert SpotifydPlayer({"spotifyd": "managd"}).mode == "off"


def test_desktop_never_names_the_player_after_the_persona():
    # Live bug 2026-11-11: desktop.py passed the persona name, so the tray
    # player showed up as "Furina" while the hub prompt said the desktop
    # player is named "Riko" — she denied her own player was online.
    src = (pathlib.Path(__file__).resolve().parents[1] / "desktop.py").read_text()
    call = re.search(r"SpotifydPlayer\(([^)]*)\)", src)
    assert call and call.group(1)
    assert "persona" not in call.group(1).lower(), call.group(1)


# ── ducking (fake props-bus; silent no-op without it) ────────────────────────

class FakeProps:
    def __init__(self, volume=0.8, status="Playing"):
        self.volume = volume
        self.status = status
        self.set_calls = []

    def Get(self, iface, key):
        return {"Volume": self.volume, "PlaybackStatus": self.status}[key]

    def Set(self, iface, key, value):
        self.set_calls.append((key, float(value)))
        if key == "Volume":
            self.volume = float(value)


class BoomProps:
    def Get(self, iface, key):
        raise RuntimeError("no such property")

    def Set(self, iface, key, value):
        raise RuntimeError("denied")


def test_duck_and_restore():
    props = FakeProps(volume=0.8)
    duck = MprisDuck(0.15, bus={"spotifyd": props})
    duck.duck()
    assert props.volume == pytest.approx(0.8 * 0.15)
    duck.restore()
    assert props.volume == pytest.approx(0.8)             # original value back


def test_double_duck_is_noop_and_restore_idempotent():
    props = FakeProps(volume=0.5)
    duck = MprisDuck(0.2, bus={"spotifyd": props})
    duck.duck()
    ducked = props.volume
    duck.duck()                                           # nested speech
    assert props.volume == pytest.approx(ducked)          # not ducked twice
    duck.restore()
    assert props.volume == pytest.approx(0.5)
    duck.restore()                                        # no-op, no crash


def test_unduckable_player_skipped():
    bad, good = BoomProps(), FakeProps(volume=0.4)
    duck = MprisDuck(0.5, bus={"spotify": bad, "spotifyd": good})
    duck.duck()
    assert good.volume == pytest.approx(0.2)
    duck.restore()


def test_no_bus_silent_noop():
    duck = MprisDuck(0.15, bus=None)                      # module default path
    monkeypatch_target = sd.SESSION_BUS
    try:
        sd.SESSION_BUS = lambda: (_ for _ in ()).throw(RuntimeError("no dbus"))
        duck.duck()                                       # must not raise
        duck.restore()
        duck.on_speak(True)
        duck.on_speak(False)
    finally:
        sd.SESSION_BUS = monkeypatch_target
    assert duck._saved == {}


def test_on_speak_hook_wires_duck_and_restore():
    props = FakeProps(volume=1.0)
    duck = MprisDuck(0.1, bus={"spotifyd": props})
    duck.on_speak(True)
    assert props.volume == pytest.approx(0.1)
    duck.on_speak(False)
    assert props.volume == pytest.approx(1.0)


# ── loopback OAuth listener ──────────────────────────────────────────────────

class FakeHub:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def _json(self, method, path, json=None):
        self.calls.append((method, path, json))
        if method == "GET":
            return {"url": "https://accounts/example?state=ST", "state": "ST"}
        if self.fail:
            raise RuntimeError("409 no handshake")
        return {"connected": True}


class TestOAuthListener:
    def test_callback_forwards_and_answers(self):
        hub = FakeHub()
        # port 0 = OS-picked; the listener builds redirect_uri from self.port
        listener = OAuthListener(hub, port=0, log=lambda *a: None)
        # port 0 binds ephemerally; recover the real port after start
        assert listener.start()
        try:
            port = listener._server.server_address[1]
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/callback?code=CODE9&state=ST",
                    timeout=5) as resp:
                page = resp.read().decode()
            assert resp.status == 200
            assert "close" in page.lower()
            method, path, payload = hub.calls[-1]
            assert (method, path) == ("POST", "/api/spotify/connect")
            assert payload["code"] == "CODE9" and payload["state"] == "ST"
            assert payload["redirect_uri"].startswith("http://127.0.0.1:")
            assert payload["redirect_uri"].endswith("/callback")
        finally:
            listener.stop()

    def test_callback_error_page_on_hub_failure(self):
        hub = FakeHub(fail=True)
        listener = OAuthListener(hub, port=0, log=lambda *a: None)
        listener.start()
        try:
            port = listener._server.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/callback?code=X&state=ST")
            try:
                urllib.request.urlopen(req, timeout=5)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            assert code == 400
            assert "hub refused" in listener.last_error or "409" in listener.last_error
        finally:
            listener.stop()

    def test_user_denied_forwarded_as_error(self):
        hub = FakeHub()
        listener = OAuthListener(hub, port=0, log=lambda *a: None)
        ok, detail = listener.handle_callback(
            "error=access_denied&error_description=user%20denied")
        assert ok is False and detail == "user denied"
        assert hub.calls == []                            # nothing POSTed
