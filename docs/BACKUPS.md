# Backups

Nightly backup of the Riko hub's real state (chat history, memory, timers,
personas, voices) plus a second copy on the PC. **As of 2026-11-10 the hub
has ZERO backups** — install the timers below to change that.

Layout:

```
eve  ~/AI/Docker/riko-hub/data      (116 MB, sqlite)  ──┐ nightly 03:20
eve  ~/AI/Docker/breeze-tts/data/voices (voice samples) ─┤
eve  ~/Backups/riko-hub/riko-hub-YYYYmmdd-HHMM.tar.zst  ←┘ keep 14 (RIKO_BACKUP_KEEP)
        │ nightly 04:40, rsync over ssh -i ~/.ssh/gpubox
        ▼
PC   ~/Backups/riko-hub/            ← the folder the owner's Duplicati on
                                       Nemu (192.0.2.101) should watch
```

Nemu cannot currently ssh to eve with this PC's keys, so the third copy is (and the PC has no `Host eve` ssh alias, so `backup_pull.sh` defaults to the literal `user@192.0.2.51:` — override with `RIKO_BACKUP_REMOTE` if you add one)
**pulled from the PC**, not pushed from eve. Point Duplicati at the PC's
`~/Backups/riko-hub`.

## What is backed up

Per archive (`scripts/backup_hub.sh`, runs on eve):

| Archive member | Source on eve | Notes |
| --- | --- | --- |
| `hub-data/` | `~/AI/Docker/riko-hub/data` (~116 MB) | the whole hub data dir: sqlite history/memory/timers, uploaded avatars, persona assets |
| `hub-snapshots/` | one file per `*.db` | consistent snapshot taken **inside the hub container with Python's sqlite3 backup API** (the image has no `sqlite3` CLI), falling back to `sqlite3 ".backup"` if a CLI exists, then to a plain copy when the container is down (or both fail — logged as a warning) |
| `breeze-voices/` | `~/AI/Docker/breeze-tts/data/voices` | the owner's voice samples only |
| `riko-hub.compose.yaml`, `riko-hub.env`, `breeze-tts.compose.yaml`, `breeze-tts.env` | deploy dirs | only if present; `.env` holds secrets → archive is mode 600, dir 700 |

Rotation keeps the newest `RIKO_BACKUP_KEEP` (default 14) archives. Log:
`~/.local/state/riko-backup.log` (eve), `~/.local/state/riko-backup-pull.log` (PC).

## What is NOT backed up (re-downloadable)

- ASR model weights (Whisper on eve / the ASR box) — re-download on restore.
- Breeze-TTS model weights under `breeze-tts/data` (~3.4 GB, mostly weights) —
  deliberately skipped; only `data/voices` is backed up.
- LLM: remote API, nothing local.
- Docker images: rebuilt from the repo (`git ls-files | tar` deploy,
  `docker compose build`).

## Restore

Full restore (eve). `ARCHIVE` = the `.tar.zst` you want:

```bash
cd ~/AI/Docker/riko-hub
docker compose down                      # stop the hub so sqlite is quiet
mv data data.broken.$(date +%s)          # keep the wreck until you're happy
mkdir -p data
tar --zstd -xf ~/Backups/riko-hub/riko-hub-YYYYmmdd-HHMM.tar.zst -C /tmp/riko-restore
cp -a /tmp/riko-restore/hub-data/. data/
# restore the env/compose files if the deploy dir was lost too:
#   cp /tmp/riko-restore/riko-hub.env .env ; cp /tmp/riko-restore/riko-hub.compose.yaml compose.yaml
# restore breeze voices if needed:
#   cp -a /tmp/riko-restore/breeze-voices/. ~/AI/Docker/breeze-tts/data/voices
docker compose up -d
```

Sqlite-only restore (keep everything else on disk): stop the hub, then copy
the consistent snapshot over the live db (the hub opens one db under `data/`;
check its name with `ls data/`):

```bash
cd ~/AI/Docker/riko-hub && docker compose stop hub
tar --zstd -xf ~/Backups/riko-hub/<archive>.tar.zst -C /tmp/riko-restore hub-snapshots
cp /tmp/riko-restore/hub-snapshots/<name>.db data/<name>.db
rm -f data/<name>.db-wal data/<name>.db-shm   # stale WAL beside a replaced db
docker compose up -d hub
```

## Install the timers

Both are **user** units (`systemctl --user`), on Fedora/eve the user session
must linger so the timer fires without a login.

### eve (backup, 03:20 + up to 15 min jitter)

```bash
# 1. script
install -m 700 -D scripts/backup_hub.sh ~/.local/bin/backup_hub.sh
# 2. units
install -m 644 -D scripts/systemd/riko-backup.service ~/.config/systemd/user/riko-backup.service
install -m 644 -D scripts/systemd/riko-backup.timer   ~/.config/systemd/user/riko-backup.timer
# 3. fire it
systemctl --user daemon-reload
systemctl --user enable --now riko-backup.timer
loginctl enable-linger "$USER"        # run without an open session
# smoke test + check:
systemctl --user start riko-backup.service
ls -l ~/Backups/riko-hub/             # expect riko-hub-*.tar.zst, mode 600
tail ~/.local/state/riko-backup.log
systemctl --user list-timers riko-backup.timer
```

### PC (pull, 04:40)

```bash
install -m 644 -D scripts/systemd/riko-backup-pull.service ~/.config/systemd/user/riko-backup-pull.service
install -m 644 -D scripts/systemd/riko-backup-pull.timer   ~/.config/systemd/user/riko-backup-pull.timer
systemctl --user daemon-reload
systemctl --user enable --now riko-backup-pull.timer
# smoke test:
systemctl --user start riko-backup-pull.service
ls -l ~/Backups/riko-hub/
```

The pull unit's `ExecStart` assumes the repo at `~/Development/riko_project`
on the PC; edit if yours lives elsewhere. `backup_pull.sh` mirrors with
`--delete`, so the PC copy rotates exactly like eve's.

## Env overrides (all optional)

`RIKO_BACKUP_DIR`, `RIKO_BACKUP_HOME`, `RIKO_BACKUP_KEEP` (eve);
`RIKO_BACKUP_REMOTE`, `RIKO_BACKUP_PULL_DIR`, `RIKO_BACKUP_SSH_KEY` (PC);
plus `RIKO_HUB_DIR`/`RIKO_BREEZE_DIR`/`RIKO_BACKUP_CONTAINER`/
`RIKO_BACKUP_TIMESTAMP`/log paths. Tests use these plus PATH shims so they
never touch the real `$HOME` and never ssh to eve.
