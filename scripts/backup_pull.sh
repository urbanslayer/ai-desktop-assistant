#!/usr/bin/env bash
# riko-backup-pull: mirror the hub backups from the backup box onto this PC.
#
# Runs on the PC (systemd user timer riko-backup-pull.timer; see
# docs/BACKUPS.md). The destination folder is the copy the owner's Duplicati
# on the second LAN box should watch — that box cannot ssh into the backup
# box with today's keys, so the second hop is pulled FROM this PC.
#
# All env vars are overridable so tests never touch the real $HOME or ssh
# to the backup box.
set -Eeuo pipefail
umask 077

BACKUP_HOME="${RIKO_BACKUP_HOME:-${HOME:?HOME unset; set RIKO_BACKUP_HOME}}"
DEST="${RIKO_BACKUP_PULL_DIR:-$BACKUP_HOME/Backups/riko-hub}"
SSH_KEY="${RIKO_BACKUP_SSH_KEY:-$BACKUP_HOME/.ssh/gpubox}"
LOG_FILE="${RIKO_BACKUP_PULL_LOG:-$BACKUP_HOME/.local/state/riko-backup-pull.log}"

# The remote is NOT hardcoded: the repo must not carry anyone's LAN layout.
# Either give the whole rsync source (RIKO_BACKUP_REMOTE, e.g.
# user@host:Backups/riko-hub/ — this is what the systemd unit should set),
# or just the host (+ user) and we build the standard hub-backup path.
REMOTE="${RIKO_BACKUP_REMOTE:-}"
if [[ -z "$REMOTE" ]]; then
  if [[ -n "${RIKO_BACKUP_HOST:-}" ]]; then
    REMOTE="${RIKO_BACKUP_USER:-${USER:?USER unset; set RIKO_BACKUP_REMOTE}}@${RIKO_BACKUP_HOST}:Backups/riko-hub/"
  else
    echo "riko-backup-pull: no remote configured — set RIKO_BACKUP_REMOTE" \
         "(e.g. RIKO_BACKUP_REMOTE=user@backupbox:Backups/riko-hub/) or" \
         "RIKO_BACKUP_HOST [+RIKO_BACKUP_USER]. See docs/BACKUPS.md." >&2
    exit 1
  fi
fi

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }
fail() { log "ERROR: $*"; echo "riko-backup-pull: $*" >&2; exit 1; }

command -v rsync >/dev/null 2>&1 || fail "rsync not found"
mkdir -p "$(dirname "$LOG_FILE")" "$DEST"
chmod 700 "$DEST"
[[ -f "$SSH_KEY" ]] || log "WARN: ssh identity file not found: $SSH_KEY"

# Trailing slash on REMOTE = mirror its CONTENTS into DEST. --delete keeps
# the local mirror aligned with eve's RIKO_BACKUP_KEEP rotation.
if rsync -a --delete --timeout=120 \
  -e "ssh -i \"$SSH_KEY\" -o BatchMode=yes -o IdentitiesOnly=yes" \
  "$REMOTE" "$DEST/"; then
  :
else
  rc=$?
  fail "rsync failed (exit $rc) pulling $REMOTE"
fi

count="$(find "$DEST" -maxdepth 1 -name 'riko-hub-*.tar.zst' 2>/dev/null | wc -l)"
log "pulled $REMOTE -> $DEST (archives on disk=$count)"
exit 0
