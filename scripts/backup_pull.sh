#!/usr/bin/env bash
# riko-backup-pull: mirror the hub backups from eve onto this PC.
#
# Runs on the PC (systemd user timer riko-backup-pull.timer; see
# docs/BACKUPS.md). The destination folder is the copy the owner's Duplicati
# on Nemu (192.0.2.101) should watch — Nemu cannot ssh into eve with
# today's keys, so the second hop is pulled FROM this PC.
#
# All env vars are overridable so tests never touch the real $HOME or ssh
# to eve.
set -Eeuo pipefail
umask 077

BACKUP_HOME="${RIKO_BACKUP_HOME:-${HOME:?HOME unset; set RIKO_BACKUP_HOME}}"
# No `Host eve` entry in ~/.ssh/config on this PC, so default to the literal
# address (the gpubox key is authorised for that user).
REMOTE="${RIKO_BACKUP_REMOTE:-${RIKO_BACKUP_USER:-user}@192.0.2.51:Backups/riko-hub/}"
DEST="${RIKO_BACKUP_PULL_DIR:-$BACKUP_HOME/Backups/riko-hub}"
SSH_KEY="${RIKO_BACKUP_SSH_KEY:-$BACKUP_HOME/.ssh/gpubox}"
LOG_FILE="${RIKO_BACKUP_PULL_LOG:-$BACKUP_HOME/.local/state/riko-backup-pull.log}"

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
