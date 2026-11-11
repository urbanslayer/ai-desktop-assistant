"""Fast tests for scripts/backup_hub.sh + scripts/backup_pull.sh.

Everything runs against a fake tree under tmp_path with PATH shims for
docker/tar/zstd/rsync/ssh: no real $HOME is ever written, no docker call,
no ssh to eve. The real `tar` binary is reused via an absolute path baked
into the tar shim so the produced ".tar.zst" is a real (uncompressed,
fake-zstd passthrough) tar we can inspect with the tarfile module.
"""
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HUB_SCRIPT = REPO / "scripts" / "backup_hub.sh"
PULL_SCRIPT = REPO / "scripts" / "backup_pull.sh"


def _write_shim(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


@pytest.fixture()
def fake_home(tmp_path):
    """A fake eve tree: hub data dir with one sqlite db + deploy files."""
    home = tmp_path / "fakehome"
    hub_data = home / "AI" / "Docker" / "riko-hub" / "data"
    hub_data.mkdir(parents=True)
    (hub_data / "history.db").write_bytes(b"LIVEDB-CONTENT")
    (home / "AI" / "Docker" / "riko-hub" / "compose.yaml").write_text("services: {}\n")
    (home / "AI" / "Docker" / "riko-hub" / ".env").write_text("SECRET=1\n")
    voices = home / "AI" / "Docker" / "breeze-tts" / "data" / "voices"
    voices.mkdir(parents=True)
    (voices / "rika.wav").write_bytes(b"RIFFfakewav")
    # model weights: must NOT end up in the archive
    weights = home / "AI" / "Docker" / "breeze-tts" / "data" / "model"
    weights.mkdir()
    (weights / "weights.bin").write_bytes(b"HEAVY" * 100)
    return home


def _hub_env(tmp_path, fake_home, monkeypatch, *, docker_mode="up", keep="14",
             timestamp="20261110-0320"):
    """Env for running backup_hub.sh: fake HOME + PATH shims."""
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    snap_src = tmp_path / "online-snapshot.db"
    snap_src.write_bytes(b"ONLINESNAP-CONTENT")
    # fake docker: `ps` reports the hub running/stopped; `exec python`
    # (sqlite3 backup API) and `exec sqlite3` succeed; `cp` copies a canned
    # consistent-snapshot file out.  docker_mode "up-cli" makes the in-container
    # `python` route fail so the sqlite3 CLI route must take over.
    python_rc = 0 if docker_mode in ("up",) else 1
    _write_shim(shim, "docker", f"""
case "$1" in
  ps) if [ "$FAKE_DOCKER_MODE" != down ]; then echo riko-hub-hub-1; fi; exit 0;;
  exec) case "$3" in python) exit {python_rc};; sqlite3|rm) exit 0;; esac; exit 1;;
  cp) shift; cp "$FAKE_SNAP_FILE" "$2"; exit 0;;
esac
exit 1
""")
    real_tar = shutil_which_or_fail("tar")
    _write_shim(shim, "tar", f'exec "{real_tar}" "$@"')
    # fake zstd: passthrough copy (so the archive is a plain tar).
    _write_shim(shim, "zstd", """
in=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -*) shift;;
    *) in="$1"; shift;;
  esac
done
cp "$in" "$out"
""")
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(fake_home)          # nothing may touch the real $HOME
    env["FAKE_DOCKER_MODE"] = docker_mode
    env["FAKE_SNAP_FILE"] = str(snap_src)
    env["RIKO_BACKUP_KEEP"] = keep
    env["RIKO_BACKUP_TIMESTAMP"] = timestamp
    monkeypatch.setenv("HOME", str(fake_home))  # belt and braces for pytest
    return env


def shutil_which_or_fail(name: str) -> str:
    import shutil
    p = shutil.which(name)
    assert p, f"{name} not available in the test environment"
    return p


def _run(script: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120)


def _archives(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("riko-hub-*.tar.zst"))


def _members(archive: Path) -> dict[str, bytes]:
    out = {}
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            if m.isfile():
                out[m.name] = tf.extractfile(m).read()
    return out


class TestBackupHub:
    def test_online_snapshot_and_archive_contents(self, tmp_path, fake_home, monkeypatch):
        env = _hub_env(tmp_path, fake_home, monkeypatch, docker_mode="up")
        r = _run(HUB_SCRIPT, env)
        assert r.returncode == 0, r.stderr
        backup_dir = fake_home / "Backups" / "riko-hub"
        archives = _archives(backup_dir)
        assert [a.name for a in archives] == ["riko-hub-20261110-0320.tar.zst"]
        # perms: dir 700, archive 600 (.env inside holds secrets)
        assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
        members = _members(archives[0])
        # whole data dir copied
        assert members["./hub-data/history.db"] == b"LIVEDB-CONTENT"
        # online sqlite .backup snapshot won over the plain copy
        assert members["./hub-snapshots/history.db"] == b"ONLINESNAP-CONTENT"
        # voices yes, model weights no
        assert members["./breeze-voices/rika.wav"] == b"RIFFfakewav"
        assert not any("weights.bin" in n or "model" in n for n in members)
        # compose/.env included under distinct names
        assert members["./riko-hub.env"] == b"SECRET=1\n"
        assert "./riko-hub.compose.yaml" in members
        # logged, no leftovers
        log = (fake_home / ".local" / "state" / "riko-backup.log").read_text()
        assert "online snapshot (python backup API): history.db" in log
        assert "finished OK" in log
        assert not list(backup_dir.glob(".tmp.*"))

    def test_sqlite_cli_route_when_python_exec_fails(self, tmp_path, fake_home, monkeypatch):
        """The hub image has no sqlite3 CLI, but if the in-container python
        route fails the sqlite3 `.backup` route must still be tried (and win
        over the plain copy)."""
        env = _hub_env(tmp_path, fake_home, monkeypatch, docker_mode="up-cli",
                       timestamp="20261110-0325")
        r = _run(HUB_SCRIPT, env)
        assert r.returncode == 0, r.stderr
        members = _members(_archives(fake_home / "Backups" / "riko-hub")[0])
        assert members["./hub-snapshots/history.db"] == b"ONLINESNAP-CONTENT"
        log = (fake_home / ".local" / "state" / "riko-backup.log").read_text()
        assert "online snapshot (.backup): history.db" in log

    def test_sqlite_fallback_when_container_down(self, tmp_path, fake_home, monkeypatch):
        env = _hub_env(tmp_path, fake_home, monkeypatch, docker_mode="down",
                       timestamp="20261110-0321")
        r = _run(HUB_SCRIPT, env)
        assert r.returncode == 0, r.stderr
        archives = _archives(fake_home / "Backups" / "riko-hub")
        members = _members(archives[0])
        # fallback: plain copy of the live db (consistent, hub is stopped)
        assert members["./hub-snapshots/history.db"] == b"LIVEDB-CONTENT"
        log = (fake_home / ".local" / "state" / "riko-backup.log").read_text()
        assert "hub down, plain copy" in log

    def test_rotation_keeps_exactly_n(self, tmp_path, fake_home, monkeypatch):
        env = _hub_env(tmp_path, fake_home, monkeypatch, keep="3")
        stamps = [f"20261110-03{m:02d}" for m in range(5)]
        for s in stamps:
            env["RIKO_BACKUP_TIMESTAMP"] = s
            r = _run(HUB_SCRIPT, env)
            assert r.returncode == 0, r.stderr
        backup_dir = fake_home / "Backups" / "riko-hub"
        names = [a.name for a in _archives(backup_dir)]
        assert len(names) == 3
        # the 3 newest survive, the 2 oldest are rotated out
        assert names == [f"riko-hub-{s}.tar.zst" for s in stamps[-3:]]
        log = (fake_home / ".local" / "state" / "riko-backup.log").read_text()
        assert f"rotated out {backup_dir / ('riko-hub-' + stamps[0] + '.tar.zst')}" in log

    def test_env_overrides_redirect_everything_off_home(self, tmp_path, fake_home, monkeypatch):
        env = _hub_env(tmp_path, fake_home, monkeypatch)
        other = tmp_path / "elsewhere"
        env["RIKO_BACKUP_HOME"] = str(other)
        env["RIKO_BACKUP_DIR"] = str(other / "bins")
        r = _run(HUB_SCRIPT, env)
        assert r.returncode == 0, r.stderr
        assert len(_archives(other / "bins")) == 1
        assert (other / ".local" / "state" / "riko-backup.log").exists()
        assert not (fake_home / "Backups").exists()

    def test_bad_keep_fails_nonzero(self, tmp_path, fake_home, monkeypatch):
        env = _hub_env(tmp_path, fake_home, monkeypatch, keep="0")
        r = _run(HUB_SCRIPT, env)
        assert r.returncode != 0
        assert "positive integer" in r.stderr


class TestBackupPull:
    @pytest.fixture()
    def pull_shims(self, tmp_path):
        shim = tmp_path / "pullshim"
        shim.mkdir()
        rsync_log = tmp_path / "rsync-argv.log"
        ssh_log = tmp_path / "ssh-argv.log"
        _write_shim(shim, "rsync", f'printf "%s\\n" "$@" >> "{rsync_log}"; exit 0')
        _write_shim(shim, "ssh", f'printf "%s\\n" "$@" >> "{ssh_log}"; exit 0')
        return shim, rsync_log, ssh_log

    def _pull_env(self, tmp_path, shim, monkeypatch, **extra):
        """Base env for backup_pull.sh: shims on PATH + the RIKO_BACKUP_*
        vars stripped (the remote must come from the test, never from the
        developer's shell) + fake $HOME."""
        env = dict(os.environ)
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        env["HOME"] = str(tmp_path / "fakehome")
        for var in ("RIKO_BACKUP_REMOTE", "RIKO_BACKUP_HOST", "RIKO_BACKUP_USER"):
            env.pop(var, None)
        env["RIKO_BACKUP_PULL_DIR"] = str(tmp_path / "dest")
        env["RIKO_BACKUP_SSH_KEY"] = str(tmp_path / "nokey")
        env["RIKO_BACKUP_PULL_LOG"] = str(tmp_path / "pull.log")
        monkeypatch.setenv("HOME", env["HOME"])
        env.update(extra)
        return env

    def test_pull_uses_remote_path_and_identity_file(self, tmp_path, pull_shims, monkeypatch):
        shim, rsync_log, ssh_log = pull_shims
        key = tmp_path / "fakehome" / ".ssh" / "gpubox"
        key.parent.mkdir(parents=True)
        key.write_text("fake-key\n")
        dest = tmp_path / "pc-backups"
        env = self._pull_env(tmp_path, shim, monkeypatch,
                             RIKO_BACKUP_REMOTE="eve:Backups/riko-hub/",
                             RIKO_BACKUP_SSH_KEY=str(key),
                             RIKO_BACKUP_PULL_DIR=str(dest))
        r = _run(PULL_SCRIPT, env)
        assert r.returncode == 0, r.stderr
        argv = rsync_log.read_text()
        assert "eve:Backups/riko-hub/" in argv        # remote source
        assert f"ssh -i \"{key}\"" in argv            # gpubox identity via ssh -i
        assert str(dest) in argv                      # local destination
        assert dest.is_dir()
        assert "pulled eve:Backups/riko-hub/" in (tmp_path / "pull.log").read_text()
        # the shim never invoked a real ssh itself
        assert not ssh_log.exists()

    def test_pull_builds_remote_from_host_and_user(self, tmp_path, pull_shims, monkeypatch):
        """No RIKO_BACKUP_REMOTE: RIKO_BACKUP_HOST (+ RIKO_BACKUP_USER)
        builds the standard hub-backup source."""
        shim, rsync_log, _ = pull_shims
        env = self._pull_env(tmp_path, shim, monkeypatch,
                             RIKO_BACKUP_HOST="backupbox",
                             RIKO_BACKUP_USER="user")
        r = _run(PULL_SCRIPT, env)
        assert r.returncode == 0, r.stderr
        assert "user@backupbox:Backups/riko-hub/" in rsync_log.read_text()

    def test_pull_requires_a_remote_env(self, tmp_path, pull_shims, monkeypatch):
        """No personal default left in the repo: unset remote -> clear error
        naming the env vars, before rsync is ever called."""
        shim, rsync_log, _ = pull_shims
        env = self._pull_env(tmp_path, shim, monkeypatch)
        r = _run(PULL_SCRIPT, env)
        assert r.returncode != 0
        assert "RIKO_BACKUP_REMOTE" in r.stderr
        assert not rsync_log.exists()

    def test_pull_fails_when_rsync_fails(self, tmp_path, pull_shims, monkeypatch):
        shim, _, _ = pull_shims
        (shim / "rsync").write_text("#!/bin/sh\nexit 255\n")
        (shim / "rsync").chmod(0o755)
        env = self._pull_env(tmp_path, shim, monkeypatch,
                             RIKO_BACKUP_REMOTE="eve:Backups/riko-hub/")
        env["RIKO_BACKUP_SSH_KEY"] = str(tmp_path / "nokey")
        r = _run(PULL_SCRIPT, env)
        assert r.returncode != 0
        assert "rsync failed" in r.stderr
