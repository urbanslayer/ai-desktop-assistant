"""Spotify Soloist desktop player (docs/SOLOIST.md): the SoloistPlayer
child-process manager, the official-CDN auto-updater, WebSocket ducking,
and the tray Music menu's Soloist items. Fakes only — fake popen for the
child (NEVER the owner's daemon), a local fake HTTP server / injected http
for the CDN (NEVER the real spotifycdn.com), a fake `soloist` shell script
where a real process is needed, a real loopback WebSocket server from the
`websockets` dependency for ducking. Fake HOME throughout so the real file
writes (~/.config/riko/soloist.key, ~/.local/share/riko/soloist) never
touch the owner's home. The key VALUE only ever appears as FAKE-KEY-...
here; tests assert it is scrubbed from surfaced text.
"""
import gzip
import io
import json
import os
import pathlib
import stat
import tarfile
import time
from datetime import date, timedelta

import pytest

from riko.music import soloist as so
from riko.music.soloist import (EXPIRED_EXIT, KEY_SETUP_TEXT, PAIR_HINT,
                                REDACTION, SoloistDuck, SoloistPlayer,
                                SoloistUpdater, SoloistWs, select_player)

FAKE_KEY = "FAKE-KEY-9e2f3a-not-a-secret"

REAL_VERSION_LINE = ("soloist 1.3.10.61 build 1794376870 (20261111) "
                     "(g19412df6ea) (linux/x86_64)")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """Fake HOME carrying a valid (0600) key; per-test code deletes or
    chmods it as needed."""
    home = tmp_path / "home"
    key = home / ".config" / "riko" / "soloist.key"
    key.parent.mkdir(parents=True)
    key.write_text(FAKE_KEY + "\n")
    os.chmod(key, 0o600)
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "RIKO_CONFIG_DIR", "RIKO_SOLOIST_HOME", "RIKO_SOLOIST_CACHE",
                "RIKO_SPOTIFYD_CACHE"):
        monkeypatch.delenv(var, raising=False)
    return home


def write_binary(player, text="#!/bin/sh\n"):
    path = player.root / "bin" / "soloist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o755)
    return path


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeProc:
    def __init__(self, argv=None, returncode=None):
        self.argv = argv
        self.alive = True
        self.terminated = False
        self.pid = 4242
        self.returncode = returncode
        self.stdout = None

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
    def __init__(self, output="", returncode=1):
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


class FakeUpdater:
    """Stands in for SoloistUpdater inside SoloistPlayer tests."""

    def __init__(self, results=None, bin_path=None, info=None):
        self.calls = []
        self.results = list(results or [{"ok": True, "updated": False}])
        self.bin_path = bin_path
        self._info = info if info is not None else {}
        self.auto_update = True

    def check(self, force=False):
        self.calls.append(force)
        if self.bin_path is not None and self.results \
                and self.results[0].get("updated"):
            self.bin_path.parent.mkdir(parents=True, exist_ok=True)
            self.bin_path.write_text("#!/bin/sh\n")
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def installed_version(self):
        return dict(self._info)


class FakeWs:
    def __init__(self, playing=False, logged_in=True, available=True):
        self.playing = playing
        self.logged_in_value = logged_in
        self.available_value = available
        self.volumes = []

    def available(self):
        return self.available_value

    def is_playing(self):
        return self.playing

    def logged_in(self):
        return self.logged_in_value

    def playback_state(self):
        return {"status": "playing" if self.playing else "paused",
                "volume": 80}

    def set_volume(self, v):
        self.volumes.append(v)
        return True


class R:
    """stand-in for subprocess.run's CompletedProcess."""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def make_tar(members):
    """members: {name: (payload|None, type, size_override)} -> tar.gz bytes.
    size_override forges the header size (oversize detection test)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, (payload, mtype, size) in members.items():
            ti = tarfile.TarInfo(name)
            ti.type = mtype
            payload = payload or b""
            ti.size = size if size is not None else len(payload)
            tar.addfile(ti, io.BytesIO(payload))
    return buf.getvalue()


def good_tar(payload=b"NEW-BINARY"):
    return make_tar({"soloist": (payload, tarfile.REGTYPE, None),
                     "CHANGELOG.md": (b"notes", tarfile.REGTYPE, None)})


class FakeHttp:
    """The injected CDN seam: programmable HEAD headers + GET body."""

    def __init__(self, etag="ETAG-1", last_modified="Tue, 11 Nov 2026 00:00:00 GMT",
                 blob=None, head_status=200, fail=False):
        self.headers = {"etag": etag, "last-modified": last_modified,
                        "content-length": str(len(blob or b""))}
        self.blob = good_tar() if blob is None else blob
        self.head_status = head_status
        self.fail = fail
        self.calls = []

    def __call__(self, url, method="GET", timeout=30.0):
        self.calls.append((method, url))
        if self.fail:
            raise OSError("no route to soloist-builds.spotifycdn.com")
        if method == "HEAD":
            return self.head_status, dict(self.headers), b""
        return 200, dict(self.headers), self.blob


def updater(**kw):
    kw.setdefault("runner", lambda argv, timeout: R(REAL_VERSION_LINE))
    kw.setdefault("log", lambda *a: None)
    return SoloistUpdater({}, **kw)


def managed_player(popen=None, music_cfg=None, **kw):
    """A SoloistPlayer with everything faked: binary present, key valid."""
    popen = popen or RecordingPopen()
    player = SoloistPlayer(music_cfg or {"soloist": {"auto_update": True}},
                           popen=popen, log=lambda *a: None,
                           updater=FakeUpdater(), **kw)
    write_binary(player)
    player._last_check = 1e12          # don't count as 'due' by accident
    return player, popen


# ── version parsing / expiry math ────────────────────────────────────────────

def test_version_line_parses_to_build_date_and_expiry():
    info = so.parse_version_output(REAL_VERSION_LINE)
    assert info["version"] == "1.3.10.61"
    assert info["build_ts"] == 1794376870
    assert info["build_date"] == date(2026, 11, 11)
    assert info["expiry"] == date(2027, 2, 9)          # +90 days exactly
    assert so.days_left(info["expiry"], date(2026, 11, 11)) == 90


def test_unparseable_version_is_not_a_version():
    assert so.parse_version_output("./soloist: not executable") == {}
    assert so.parse_version_output("") == {}
    assert so.parse_version_output("soloist 1.0 build 1 (20261399)") == {}


def test_archive_url_per_arch_and_https_only():
    assert so.archive_url("x86_64") == \
        "https://soloist-builds.spotifycdn.com/soloist_release_x86_64.tar.gz"
    assert so.archive_url("aarch64").endswith("_arm64.tar.gz")
    assert so.archive_url("armv7l").endswith("_arm32.tar.gz")
    assert so.archive_url("riscv64") == ""             # never guess


# ── key file + redaction ─────────────────────────────────────────────────────

def test_missing_key_blocks_start_with_exact_instructions(fake_home):
    (fake_home / ".config" / "riko" / "soloist.key").unlink()
    player, popen = managed_player()
    out = player.start()
    assert popen.calls == []                           # never spawn sans key
    assert out["ok"] is False
    assert "developer.spotify.com/dashboard/soloist" in out["message"]
    assert "install -D -m 600 /dev/stdin ~/.config/riko/soloist.key" \
        in out["message"]
    assert player.status()["key_error"] == KEY_SETUP_TEXT


def test_empty_key_is_missing_key(fake_home):
    key = fake_home / ".config" / "riko" / "soloist.key"
    key.write_text("   \n")
    player, popen = managed_player()
    assert "dashboard/soloist" in player.start()["message"]
    assert popen.calls == []


def test_group_readable_key_refused_with_fix_it(fake_home):
    key = fake_home / ".config" / "riko" / "soloist.key"
    os.chmod(key, 0o644)
    player, popen = managed_player()
    out = player.start()
    assert popen.calls == []
    assert "chmod 600" in out["message"] and str(key) in out["message"]
    assert FAKE_KEY not in out["message"]


def test_redaction_scrubs_the_key_from_surfaced_text(fake_home):
    popen = RecordingPopen(factory=lambda argv: DyingProc(
        f"error: api-key {FAKE_KEY} rejected by Spotify\n", returncode=1))
    player = SoloistPlayer({"soloist": {}}, popen=popen,
                           log=lambda *a: None, updater=FakeUpdater())
    write_binary(player)
    out = player.start()
    assert out["ok"] is False
    assert FAKE_KEY not in out["message"]              # never surfaces
    assert REDACTION in out["message"]
    assert "rejected by Spotify" in out["message"]     # the real reason stays
    scrubbed, hits = so.redact_key(f"stderr had {FAKE_KEY} twice: {FAKE_KEY}",
                                   FAKE_KEY)
    assert FAKE_KEY not in scrubbed and hits == 2


# ── start / stop / argv ──────────────────────────────────────────────────────

def test_managed_start_argv(fake_home):
    player, popen = managed_player()
    out = player.start()
    assert out["ok"] and "Riko" in out["message"]
    argv = popen.calls[0].argv
    assert argv[0] == str(player.root / "bin" / "soloist")
    assert argv[argv.index("-n") + 1] == "Riko"
    assert argv[argv.index("-k") + 1] == FAKE_KEY      # per-start key (docs)
    assert argv[argv.index("-D") + 1] == str(player.data_dir)
    assert argv[argv.index("-C") + 1] == str(pathlib.Path(
        fake_home / ".cache" / "riko" / "soloist"))
    assert argv[argv.index("-z") + 1] == "500"         # default cache size
    assert argv[argv.index("-w") + 1] == "127.0.0.1:0"  # loopback, OS port
    assert "-i" not in argv and "-d" not in argv       # unset = not passed
    st = player.status()
    assert st["running"] and st["pid"] == 4242 and st["kind"] == "soloist"
    stop = player.stop()
    assert stop["ok"] and popen.calls[0].terminated
    assert player.stop()["message"] == "player is not running"


def test_optional_audio_flags_passthrough(fake_home):
    player, popen = managed_player(music_cfg={"soloist": {
        "initial_volume": 60, "pipewire_device": "alsa_out.1",
        "cache_size_mb": 300}})
    assert player.start()["ok"]
    argv = popen.calls[0].argv
    assert argv[argv.index("-i") + 1] == "60"
    assert argv[argv.index("-d") + 1] == "alsa_out.1"
    assert argv[argv.index("-z") + 1] == "300"


def test_device_name_defaults_to_riko():
    assert SoloistPlayer({}).device_name == "Riko"
    assert SoloistPlayer({}, "  ").device_name == "Riko"


def test_start_installs_from_cdn_when_binary_missing(fake_home):
    http = FakeHttp()
    upd = updater(http=http)
    popen = RecordingPopen()
    player = SoloistPlayer({"soloist": {}}, popen=popen,
                           log=lambda *a: None, updater=upd)
    assert player.start()["ok"]
    assert player.find_binary() and os.access(player.find_binary(), os.X_OK)
    assert len(popen.calls) == 1
    assert ("GET", upd.url) in http.calls


def test_auto_update_off_without_binary_does_not_download(fake_home):
    http = FakeHttp()
    popen = RecordingPopen()
    player = SoloistPlayer({"soloist": {"auto_update": False}}, popen=popen,
                           log=lambda *a: None, updater=updater(http=http))
    out = player.start()
    assert out["ok"] is False and "auto_update" in out["message"]
    assert popen.calls == [] and http.calls == []


def test_early_exit_surfaces_soloists_error_and_data_dir_busy():
    popen = RecordingPopen(factory=lambda argv: DyingProc(
        "error: another soloist process already using the data directory",
        returncode=1))
    player = SoloistPlayer({"soloist": {}}, popen=popen,
                           log=lambda *a: None, updater=FakeUpdater())
    write_binary(player)
    out = player.start()
    assert out["ok"] is False and "code 1" in out["message"]
    assert "another Soloist is already running" in out["message"]


def test_first_pair_hint_on_start_without_session(fake_home):
    ws = FakeWs(logged_in=False)
    player, popen = managed_player(ws=ws)
    out = player.start()
    assert out["ok"] and "pick 'Riko' in the device list once" in out["message"]
    assert PAIR_HINT in out["message"]


# ── exit 10 = expired build ──────────────────────────────────────────────────

def test_exit_10_forces_update_and_restarts_once(fake_home):
    state = {"n": 0}

    def factory(argv):
        state["n"] += 1
        if state["n"] == 1:
            return DyingProc("build expired", returncode=EXPIRED_EXIT)
        return FakeProc(argv)

    popen = RecordingPopen(factory=factory)
    player = SoloistPlayer({"soloist": {}}, popen=popen,
                           log=lambda *a: None,
                           updater=FakeUpdater(
                               results=[{"ok": True, "updated": True,
                                         "version": "1.4.0"}]))
    write_binary(player)                        # player.root/bin/soloist
    player.updater.bin_path = player.root / "bin" / "soloist"
    out = player.start()
    assert out["ok"] is True                     # restarted on the new build
    assert len(popen.calls) == 2                 # one restart, no retry loop
    assert player.updater.calls == [True]        # forced the update


def test_exit_10_update_failure_names_the_reason(fake_home):
    popen = RecordingPopen(factory=lambda argv: DyingProc(
        "build expired", returncode=EXPIRED_EXIT))
    player = SoloistPlayer({"soloist": {}}, popen=popen,
                           log=lambda *a: None,
                           updater=FakeUpdater(results=[
                               {"ok": False, "updated": False,
                                "kind": "network", "reason": "HEAD failed"}]))
    write_binary(player)
    out = player.start()
    assert out["ok"] is False
    assert "Soloist build expired and the update failed: HEAD failed" \
        in out["message"]


# ── the updater ──────────────────────────────────────────────────────────────

def test_updater_happy_path_then_unchanged_etag(fake_home, tmp_path):
    http = FakeHttp(etag="ETAG-1")
    upd = updater(http=http)
    res = upd.check()
    assert res["ok"] and res["updated"] and res["version"] == "1.3.10.61"
    assert upd.bin_path.exists() and os.access(upd.bin_path, os.X_OK)
    meta = upd.release_info()
    assert meta["etag"] == "ETAG-1" and meta["version"] == "1.3.10.61"
    assert meta["build_date"] == "2026-11-11"
    assert len(meta["sha256"]) == 64 and meta["installed_at"] > 0
    # unchanged etag/last-modified: HEAD only, no download
    calls = len(http.calls)
    again = upd.check()
    assert again["ok"] and not again["updated"]
    assert [m for m, _ in http.calls[calls:]] == ["HEAD"]
    # changed etag: downloads again
    http.headers["etag"] = "ETAG-2"
    assert upd.check()["updated"]
    assert upd.release_info()["etag"] == "ETAG-2"


def test_updater_url_is_official_cdn_only():
    upd = updater()
    assert upd.url.startswith("https://soloist-builds.spotifycdn.com/")


def _leaves_old_binary_intact(upd, old=b"OLD-BINARY"):
    upd.bin_path.parent.mkdir(parents=True, exist_ok=True)
    upd.bin_path.write_bytes(old)
    upd.release_path.write_text(json.dumps(
        {"etag": "KEEP", "last_modified": "x", "version": "0.0.0.1",
         "build_date": "2026-01-01"}))


def test_bad_archives_leave_the_old_binary(fake_home):
    cases = {
        "symlink member": make_tar({"soloist": (b"", tarfile.SYMTYPE, None)}),
        "no soloist member": make_tar(
            {"CHANGELOG.md": (b"x", tarfile.REGTYPE, None)}),
        "absolute path member": make_tar(
            {"/soloist": (b"x", tarfile.REGTYPE, None)}),
        "dotdot path member": make_tar(
            {"../soloist": (b"x", tarfile.REGTYPE, None)}),
        "oversize member": make_tar(
            {"soloist": (b"\0" * (so.DOWNLOAD_MAX_BYTES + 1),
                         tarfile.REGTYPE, None)}),
        "not a tar.gz": b"this is not a tar.gz at all",
    }
    for label, blob in cases.items():
        upd = updater(http=FakeHttp(blob=blob))
        _leaves_old_binary_intact(upd)
        res = upd.check(force=True)
        assert not res["ok"], f"{label}: {res}"
        assert upd.bin_path.read_bytes() == b"OLD-BINARY", label
        assert json.loads(upd.release_path.read_text())["etag"] == "KEEP"


@pytest.mark.parametrize("bad", [
    R("soloist: cannot execute binary file", returncode=126),
    R("soloist 9.9 build 1 (19700101) (linux/x86_64)"),      # expired build
    R(REAL_VERSION_LINE, returncode=1),                        # nonzero exit
])
def test_failing_version_check_leaves_old_binary(fake_home, bad):
    upd = updater(http=FakeHttp(), runner=lambda argv, timeout: bad)
    _leaves_old_binary_intact(upd)
    res = upd.check(force=True)
    assert not res["ok"] and res["kind"] == "verify"
    assert upd.bin_path.read_bytes() == b"OLD-BINARY"
    assert upd.release_info()["etag"] == "KEEP"


def test_downloaded_binary_actually_executes_the_version_probe(fake_home):
    # the runner seam off: a real fake `soloist` shell script proves the
    # argv/exec/chmod-755 path end to end (NEVER the owner's real binary)
    script = fake_home / "soloist-src"
    script.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then\n"
                      f"  echo '{REAL_VERSION_LINE}'\n  exit 0\nfi\nexit 1\n")
    script.chmod(0o755)
    upd = updater(http=FakeHttp(),
                  runner=lambda argv, timeout: __import__("subprocess").run(
                      argv, capture_output=True, text=True, timeout=timeout))
    # point the staging probe at the script by feeding it as the member
    blob = make_tar({"soloist": (script.read_bytes(), tarfile.REGTYPE, None)})
    upd.http.blob = blob
    upd.http.headers["content-length"] = str(len(blob))
    res = upd.check()
    assert res["ok"] and res["updated"] and res["version"] == "1.3.10.61"


def test_size_cap_rejects_giant_content_length(fake_home):
    http = FakeHttp()
    http.headers["content-length"] = str(so.DOWNLOAD_MAX_BYTES + 1)
    upd = updater(http=http)
    res = upd.check()
    assert not res["ok"] and "cap" in res["reason"]
    assert ("GET", upd.url) not in http.calls      # never downloads it


def test_head_failure_and_auto_update_off(fake_home):
    upd = updater(http=FakeHttp(fail=True))
    res = upd.check()
    assert not res["ok"] and res["kind"] == "network"
    # same failure kind is logged once
    logs = []
    upd2 = SoloistUpdater({}, http=FakeHttp(head_status=500),
                          log=logs.append,
                          runner=lambda *a, **k: R(REAL_VERSION_LINE))
    upd2.check(); upd2.check()
    assert sum("HTTP 500" in l for l in logs) == 1
    # auto_update off + installed binary: skipped without any request
    upd3 = SoloistUpdater({"soloist": {"auto_update": False}},
                          http=FakeHttp(),
                          log=lambda *a: None,
                          runner=lambda *a, **k: R(REAL_VERSION_LINE))
    _leaves_old_binary_intact(upd3)
    assert upd3.check()["skipped"] == "auto_update off"


def test_installed_version_falls_back_to_running_the_binary(fake_home):
    upd = updater()
    upd.bin_path.parent.mkdir(parents=True, exist_ok=True)
    (upd.bin_path).write_text("#!/bin/sh\n")           # no release.json
    seen = []

    def runner(argv, timeout):
        seen.append(list(argv))
        return R(REAL_VERSION_LINE)
    upd.runner = runner
    info = upd.installed_version()
    assert info["version"] == "1.3.10.61"
    assert seen[0] == [str(upd.bin_path), "--version"]


# ── check_update restart policy ──────────────────────────────────────────────

def _far_expiry():
    return {"version": "1.3.10.61", "build_ts": 1,
            "build_date": date.today() - timedelta(days=30),
            "expiry": date.today() + timedelta(days=60)}


def test_update_defers_restart_while_playing_then_restarts_idle(fake_home):
    ws = FakeWs(playing=True)
    popen = RecordingPopen()
    upd = FakeUpdater(results=[{"ok": True, "updated": True, "version": "2"},
                               {"ok": True, "updated": False}])
    player, popen = managed_player(popen=popen, ws=ws)
    upd.bin_path = player.root / "bin" / "soloist"
    player.updater = upd
    player.start()
    res = player.check_update()
    assert res.get("deferred") and not popen.calls[0].terminated
    assert player.running()                            # playback untouched
    ws.playing = False
    res2 = player.check_update()
    assert res2.get("restarted") is True               # idle -> restart now
    assert popen.calls[0].terminated and len(popen.calls) == 2


def test_update_restarts_immediately_when_expiry_within_3_days(fake_home):
    ws = FakeWs(playing=True)
    upd = FakeUpdater(results=[{"ok": True, "updated": True, "version": "2"}],
                      info={"version": "1.0", "build_ts": 1,
                            "build_date": date.today() - timedelta(days=88),
                            "expiry": date.today() + timedelta(days=2)})
    player, popen = managed_player(popen=RecordingPopen(), ws=ws)
    player.updater = upd
    player.start()
    res = player.check_update()
    assert res.get("restarted") is True and not res.get("deferred")


def test_periodic_check_due_uses_injected_clock(fake_home):
    now = [1_000_000.0]
    popen = RecordingPopen()
    player = SoloistPlayer({"soloist": {}}, popen=popen,
                           log=lambda *a: None, updater=FakeUpdater(),
                           clock=lambda: now[0])
    write_binary(player)
    assert player.due()                                # never checked yet
    player.check_update()
    assert not player.due()
    now[0] += so.CHECK_INTERVAL_S
    assert player.due()                                # 6 h later


# ── player selection (back-compat) ───────────────────────────────────────────

def test_select_player_precedence_and_back_compat():
    assert select_player({}) == "soloist"              # shipped default
    assert select_player({"spotifyd": "managed"}) == "soloist"
    assert select_player({"spotifyd": "external"}) == "external"
    assert select_player({"spotifyd": "off"}) == "off"
    assert select_player({"player": "spotifyd",
                          "spotifyd": "managed"}) == "spotifyd"
    assert select_player({"player": "external"}) == "external"
    assert select_player({"player": "off"}) == "off"
    assert select_player({"player": "soloist"}) == "soloist"
    assert select_player({"player": "solist"}) == "off"   # typo safety


def test_player_mode_mapping_and_off():
    assert SoloistPlayer({"player": "soloist"}).mode == "managed"
    assert SoloistPlayer({"player": "external"}).mode == "external"
    off = SoloistPlayer({"player": "off"}, popen=RecordingPopen())
    assert off.mode == "off" and "off" in off.start()["message"]
    ext = SoloistPlayer({"player": "external"}, popen=RecordingPopen())
    assert "external" in ext.start()["message"]
    assert "won't stop it" in ext.stop()["message"]
    popen = RecordingPopen()
    ext2 = SoloistPlayer({"player": "external"}, popen=popen)
    ext2.start()
    assert popen.calls == []                           # external never spawns


# ── WebSocket ducking (real loopback ws server; official protocol) ───────────

class WsHub:
    """A miniature Soloist WebSocket API: auth_state on connect, then
    answers get_state / get_auth_state / set_volume like the daemon."""

    def __init__(self):
        self.volume = 80
        self.logged_in = True
        self.status = "playing"

    def handler(self, ws):
        ws.send(json.dumps({"type": "auth_state", "logged_in": self.logged_in,
                            "is_active": True, "device_name": "Riko"}))
        if self.logged_in:
            ws.send(json.dumps({"type": "playback_state",
                                "status": self.status, "volume": self.volume}))
        for raw in ws:
            msg = json.loads(raw)
            cmd = msg.get("command")
            if cmd == "get_auth_state":
                ws.send(json.dumps({"type": "auth_state",
                                    "logged_in": self.logged_in,
                                    "is_active": True,
                                    "device_name": "Riko"}))
            elif cmd == "get_state":
                ws.send(json.dumps({"type": "playback_state",
                                    "status": self.status,
                                    "volume": self.volume}))
            elif cmd == "set_volume":
                self.volume = msg["volume"]
                ws.send(json.dumps({"type": "volume_changed",
                                    "volume": self.volume}))


@pytest.fixture
def ws_server(tmp_path):
    try:
        from websockets.sync.server import serve
    except ImportError:
        pytest.skip("websockets sync server unavailable")
    hub = WsHub()

    def _serve(ws):
        try:
            hub.handler(ws)
        except Exception:
            pass

    server = serve(_serve, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ws.addr").write_text("127.0.0.1")
    (data_dir / "ws.port").write_text(str(port))
    yield hub, data_dir
    server.shutdown()


def test_ws_endpoint_discovery_and_loopback_guard(ws_server, tmp_path):
    hub, data_dir = ws_server
    ws = SoloistWs(data_dir)
    assert ws.available() and ws.logged_in() is True
    assert ws.playback_state()["volume"] == 80
    missing = SoloistWs(tmp_path / "gone")
    assert not missing.available()
    (data_dir / "ws.addr").write_text("0.0.0.0")       # NOT loopback: the
    assert not ws.available()                          # API has zero auth


def test_duck_lowers_and_restores_via_websocket(ws_server):
    hub, data_dir = ws_server
    duck = SoloistDuck(0.25, ws=SoloistWs(data_dir))
    duck.on_speak(True)
    assert hub.volume == 20                            # 80 * 0.25
    duck.on_speak(True)                                # nested speech: once
    assert hub.volume == 20
    duck.on_speak(False)
    assert hub.volume == 80                            # exact restore
    duck.on_speak(False)                               # idempotent


def test_duck_without_endpoint_is_silent_noop(tmp_path):
    duck = SoloistDuck(0.15, ws=SoloistWs(tmp_path / "nowhere"))
    duck.on_speak(True)
    duck.on_speak(False)                               # must not raise


# ── the tray menu (offscreen Qt) ─────────────────────────────────────────────

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
             "SWAYSOCK"):
    os.environ.pop(_var, None)

from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import desktop  # noqa: E402

MENU_TEXTS = ("DISPLAY", "WAYLAND_DISPLAY")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(["pytest-soloist"])


class MenuPlayer:
    kind = "soloist"
    mode = "managed"

    def __init__(self, status):
        self._status = status

    def status(self):
        return dict(self._status)


def _menu_texts(menu):
    from PySide6.QtGui import QAction
    return [a.text() for a in menu.actions() if isinstance(a, QAction)]


def _build_menu(player):
    """build_music_menu parents the submenu to the menu we hand it; keep a
    Python reference to the parent or Shiboken deletes both."""
    parent = QMenu()
    menu = desktop.build_music_menu(parent, player, notify=lambda *a: None)
    return parent, menu


def _base_status(**kw):
    st = {"mode": "managed", "running": True, "pid": 1, "binary": "/x/soloist",
          "device_name": "Riko", "setup_needed": False, "login_needed": False,
          "kind": "soloist", "version": "1.3.10.61", "expiry": "2027-02-09",
          "days_left": 90, "key_error": "", "update_pending": False,
          "detail": ""}
    st.update(kw)
    return st


def test_music_menu_status_line_and_update_item(qapp):
    parent, menu = _build_menu(MenuPlayer(_base_status()))
    texts = _menu_texts(menu)
    assert any("Soloist 1.3.10.61, expires 2027-02-09" in t for t in texts)
    assert "Update Soloist now" in texts
    assert "Start player" in texts and "Stop player" in texts


def test_music_menu_warns_near_expiry(qapp):
    parent, menu = _build_menu(MenuPlayer(_base_status(days_left=3)))
    line = next(t for t in _menu_texts(menu) if t.startswith("Soloist "))
    assert "⚠" in line and "3d" in line


def test_music_menu_key_missing_line(qapp):
    parent, menu = _build_menu(
        MenuPlayer(_base_status(version="", key_error=KEY_SETUP_TEXT)))
    assert any("key missing" in t for t in _menu_texts(menu))


def test_music_menu_off_returns_none(qapp):
    class Off:
        mode = "off"
    assert desktop.build_music_menu(QMenu(), Off()) is None


def test_desktop_never_names_the_player_after_the_persona():
    # kept from the spotifyd suite, widened: neither player construction may
    # take the persona name (live bug 2026-11-11, docs/SPOTIFY_NOTES.md)
    src = (pathlib.Path(__file__).resolve().parents[1] / "desktop.py").read_text()
    for call in ("SpotifydPlayer(", "SoloistPlayer("):
        idx = src.index("music_player = ")
        seg = src[idx:idx + 1200]
        assert "persona" not in seg.lower(), call


# ── reviewer hardening (2026-11-11) ──────────────────────────────────────────

def test_default_http_refuses_a_redirect_off_the_official_cdn(monkeypatch):
    import urllib.request as ur
    from riko.music import soloist as so

    class Resp:
        status = 200
        headers = {}
        def geturl(self):
            return "https://evil.example/soloist_release_x86_64.tar.gz"
        def read(self, n=-1):
            return b"x"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ur, "urlopen", lambda req, timeout=0: Resp())
    with pytest.raises(OSError, match="off the official CDN"):
        so._default_http(so.archive_url() if hasattr(so, "archive_url")
                         else f"https://{so.CDN_HOST}/x.tar.gz", "HEAD")


def test_cache_size_below_soloist_minimum_is_raised_to_100():
    from riko.music.soloist import SoloistPlayer
    for given, want in ((50, "100"), (0, "0"), (500, "500")):
        p = SoloistPlayer({"player": "soloist", "soloist": {"cache_size_mb": given}})
        argv = p.argv("K")
        assert argv[argv.index("-z") + 1] == want, (given, argv)


# ── ticker fast path: the playback-event feed (docs/SPOTIFY_NOTES.md) ───────

def _ws_item(uri="spotify:track:A", name="Bohemian Rhapsody",
             artists=("Queen",), album="A Night at the Opera",
             duration=354000):
    """An entity envelope shaped like the official WS docs' `item`."""
    return {"uri": uri, "entity_type": "track",
            "decorations": {
                "identity": {"name": name},
                "playback": {"duration_ms": duration},
                "creators": [{"entity": {"entity_type": "artist",
                                         "decorations":
                                         {"identity": {"name": a}}}}
                             for a in artists],
                "parent": {"entity": {"entity_type": "album",
                                      "decorations":
                                      {"identity": {"name": album}}}}}}


def _feed(tmp_path, msgs, **kw):
    """A feed on a fake connection: ws endpoint files + the connect seam
    deliver `msgs` (dicts) then go quiet. Returns (feed, beats, conn)."""
    import threading

    class FakeConn:
        def __init__(self):
            self.closed = False
            self.sent = []
            self._q = list(msgs)
            self._lock = threading.Lock()

        def send(self, data):
            self.sent.append(data)

        def recv(self, timeout=None):
            with self._lock:
                if self._q:
                    return json.dumps(self._q.pop(0))
            raise TimeoutError            # quiet, like the real seam

        def close(self):
            self.closed = True

    data_dir = tmp_path / "wsfeed"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "ws.addr").write_text("127.0.0.1")
    (data_dir / "ws.port").write_text("59999")
    conns = []

    def connect(uri):
        conn = FakeConn()
        conns.append(conn)
        return conn
    beats = []
    ws = SoloistWs(data_dir, connect=connect)
    feed = so.SoloistFeed(ws, beats.append, log=lambda *a: None,
                          retry_s=0.05, recv_timeout=0.05, **kw)
    return feed, beats, conns


def test_feed_snapshot_maps_the_official_entity_shapes(tmp_path):
    beats = []
    feed = so.SoloistFeed(None, beats.append, log=lambda *a: None)
    assert feed.feed({"type": "auth_state", "logged_in": True,
                      "is_active": True, "device_name": "Riko"}) is False
    assert feed.feed({"type": "playback_state", "status": "playing",
                      "item": _ws_item(),
                      "context": {"uri": "spotify:album:X"},
                      "position": {"position_ms": 4200}}) is True
    snap = beats[-1]
    assert snap == {"playing": True, "uri": "spotify:track:A",
                    "track": "Bohemian Rhapsody", "artists": "Queen",
                    "album": "A Night at the Opera", "duration_ms": 354000,
                    "position_ms": 4200, "device": "Riko",
                    "context_uri": "spotify:album:X"}
    # quiet events never repaint the ticker
    assert feed.feed({"type": "position_sync",
                      "position": {"position_ms": 9000}}) is False
    assert feed.feed({"type": "volume_changed", "volume": 40}) is False
    # same uri again (a stray repeat of playback_state): deduped
    assert feed.feed({"type": "playback_state", "status": "playing",
                      "item": _ws_item(), "position": {}}) is False
    # pause paints (paused-with-track keeps the bar visible)
    assert feed.feed({"type": "playback_changed",
                      "status": "paused"}) is True
    assert beats[-1]["playing"] is False and beats[-1]["uri"] == "spotify:track:A"
    # idle = the hub's hide signal
    assert feed.feed({"type": "playback_changed", "status": "idle"}) is True
    assert beats[-1]["uri"] == "" and beats[-1]["playing"] is False
    # resume: playback_changed(playing) repaints the still-loaded item
    assert feed.feed({"type": "playback_changed", "status": "playing"}) is True
    assert beats[-1]["uri"] == "spotify:track:A"
    # granular track_changed carries the new item
    assert feed.feed({"type": "track_changed", "item": _ws_item(
        uri="spotify:track:B", name="Under Pressure",
        artists=("Queen", "Bowie"))}) is True
    assert beats[-1]["uri"] == "spotify:track:B"
    assert beats[-1]["artists"] == "Queen, Bowie"
    # a track_changed while idle does NOT paint (the hub's shape rule:
    # idle means empty; the real player follows with playback_changed)
    idle_feed = so.SoloistFeed(None, beats.append, log=lambda *a: None)
    idle_feed.feed({"type": "auth_state", "logged_in": True,
                    "is_active": True, "device_name": "Riko"})
    idle_feed.feed({"type": "playback_state", "status": "playing",
                    "item": _ws_item()})
    idle_feed.feed({"type": "playback_changed", "status": "idle"})
    n = len(beats)
    assert idle_feed.feed({"type": "track_changed",
                           "item": _ws_item(uri="spotify:track:C")}) is False
    assert len(beats) == n


def test_feed_stays_silent_while_another_device_is_active(tmp_path):
    beats = []
    feed = so.SoloistFeed(None, beats.append, log=lambda *a: None)
    feed.feed({"type": "auth_state", "logged_in": True,
               "is_active": False, "device_name": "Riko"})
    assert feed.feed({"type": "playback_state", "status": "idle",
                      "item": None}) is False       # not our device to paint
    feed.feed({"type": "device_changed", "is_active": True,
               "device_name": "Riko"})
    assert feed.feed({"type": "playback_state", "status": "playing",
                      "item": _ws_item()}) is True  # transfer IN: now we paint


def test_feed_thread_drives_page_from_playback_events(tmp_path):
    """Fake WS connection pushing the official event sequence: the feed
    thread fires the callback for real changes only (the desktop closure
    turns a beat into a page paint + hub nudge)."""
    msgs = [{"type": "auth_state", "logged_in": True, "is_active": True,
             "device_name": "Riko"},
            {"type": "playback_state", "status": "playing",
             "item": _ws_item(), "position": {"position_ms": 10}},
            {"type": "volume_changed", "volume": 11},
            {"type": "track_changed", "item": _ws_item(
                uri="spotify:track:B", name="I Want It All")}]
    feed, beats, conns = _feed(tmp_path, msgs)
    feed.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(beats) < 2:
        time.sleep(0.02)
    feed.stop()
    assert [b["uri"] for b in beats] == ["spotify:track:A", "spotify:track:B"]
    assert conns and conns[0].closed is True        # stop closed the socket


def test_feed_survives_dropped_connections_and_missing_files(tmp_path):
    import threading

    data_dir = tmp_path / "wsfeed2"
    data_dir.mkdir(parents=True, exist_ok=True)      # NO ws.addr/ws.port yet
    seen = {"opens": 0}

    class Boom:
        def send(self, data):
            pass

        def recv(self, timeout=None):
            raise ConnectionError("soloist restarted")

        def close(self):
            pass

    def connect(uri):
        seen["opens"] += 1
        return Boom()
    ws = SoloistWs(data_dir, connect=connect)
    feed = so.SoloistFeed(ws, lambda s: None, log=lambda *a: None,
                          retry_s=0.02, recv_timeout=0.02)
    feed.start()                                     # endpoint down: no crash
    time.sleep(0.1)
    assert seen["opens"] == 0                        # nothing to open yet
    (data_dir / "ws.addr").write_text("127.0.0.1")
    (data_dir / "ws.port").write_text("59998")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and seen["opens"] < 2:
        time.sleep(0.02)                             # reconnect loop churns
    feed.stop()
    assert seen["opens"] >= 2                        # never fatal, keeps trying
