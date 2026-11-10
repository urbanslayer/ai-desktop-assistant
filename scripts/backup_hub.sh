#!/usr/bin/env bash
# riko-backup: nightly snapshot of the Riko hub data on eve.
#
# Runs ON EVE (systemd user timer riko-backup.timer; see docs/BACKUPS.md).
# Produces  ~/Backups/riko-hub/riko-hub-YYYYmmdd-HHMM.tar.zst  containing:
#   - hub-data/           full ~/AI/Docker/riko-hub/data (sqlite + everything)
#   - hub-snapshots/      consistent sqlite snapshots (.backup) per *.db
#   - breeze-voices/      ~/AI/Docker/breeze-tts/data/voices (voice samples;
#                         the 3.4 GB model weights are deliberately skipped)
#   - riko-hub.compose.yaml / riko-hub.env / breeze-tts.compose.yaml /
#     breeze-tts.env      if present (.env holds secrets -> file mode 600)
#
# Rotation: keep the newest RIKO_BACKUP_KEEP (default 14) archives.
# Every env var below is overridable so tests can run against a fake tree
# without touching $HOME or talking to docker.
set -Eeuo pipefail
umask 077

BACKUP_HOME="${RIKO_BACKUP_HOME:-${HOME:?HOME unset; set RIKO_BACKUP_HOME}}"
BACKUP_DIR="${RIKO_BACKUP_DIR:-$BACKUP_HOME/Backups/riko-hub}"
KEEP="${RIKO_BACKUP_KEEP:-14}"
LOG_FILE="${RIKO_BACKUP_LOG:-$BACKUP_HOME/.local/state/riko-backup.log}"

HUB_DIR="${RIKO_HUB_DIR:-$HOME/AI/Docker/riko-hub}"
HUB_DATA="${RIKO_HUB_DATA_DIR:-$HUB_DIR/data}"
BREEZE_DIR="${RIKO_BREEZE_DIR:-$HOME/AI/Docker/breeze-tts}"
# Container holding the live sqlite files; empty = auto-detect via docker ps.
CONTAINER="${RIKO_BACKUP_CONTAINER:-}"
# Where the hub data dir is mounted INSIDE the container (hub/compose.yaml).
CONTAINER_DATA="${RIKO_BACKUP_CONTAINER_DATA:-/data}"
TIMESTAMP="${RIKO_BACKUP_TIMESTAMP:-$(date +%Y%m%d-%H%M)}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }
fail() { log "ERROR: $*"; echo "riko-backup: $*" >&2; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
[[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP >= 1 )) || fail "RIKO_BACKUP_KEEP must be a positive integer (got: $KEEP)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

trap 'rc=$?; (( rc == 0 )) || { log "ERROR: backup_hub.sh failed (exit $rc)"; exit "$rc"; }' EXIT

[[ -d "$HUB_DATA" ]] || fail "hub data dir not found: $HUB_DATA"
command -v tar >/dev/null 2>&1 || fail "tar not found"
command -v zstd >/dev/null 2>&1 || fail "zstd not found"

# ── resolve the running hub container (empty = hub is down) ────────────────
if [[ -z "$CONTAINER" ]] && command -v docker >/dev/null 2>&1; then
  CONTAINER="$(docker ps --filter 'name=riko-hub' --filter 'status=running' \
      --format '{{.Names}}' 2>/dev/null | head -n1 || true)"
fi

TMP="$(mktemp -d "$BACKUP_DIR/.tmp.XXXXXX")"
trap 'rc=$?; rm -rf "${TMP:-}"; (( rc == 0 )) || { log "ERROR: backup_hub.sh failed (exit $rc)"; exit "$rc"; }' EXIT
STAGE="$TMP/stage" SNAP="$TMP/snapshots"
mkdir -p "$STAGE" "$SNAP"

# ── hub data dir ───────────────────────────────────────────────────────────
cp -a "$HUB_DATA" "$STAGE/hub-data"

# ── consistent sqlite snapshots ────────────────────────────────────────────
# Preferred: `docker exec <hub> sqlite3 <db> .backup` (safe while writing).
# Fallback: plain file copy — consistent when the container is down; logged
# as possibly-mid-write when it is up but .backup failed.
while IFS= read -r -d '' db; do
  rel="${db#"$HUB_DATA"/}"
  name="${rel//\//_}"
  ok=0
  if [[ -n "$CONTAINER" ]] && command -v docker >/dev/null 2>&1; then
    in_tmp="/tmp/riko-snap-$$-$name"
    if docker exec "$CONTAINER" sqlite3 "$CONTAINER_DATA/$rel" ".backup '$in_tmp'" 2>/dev/null \
       && docker cp "$CONTAINER:$in_tmp" "$SNAP/$name" 2>/dev/null; then
      docker exec "$CONTAINER" rm -f "$in_tmp" 2>/dev/null || true
      ok=1
      log "sqlite online snapshot (.backup): $rel"
    else
      log "WARN: online .backup failed for $rel; falling back to a file copy"
    fi
  fi
  if (( ! ok )); then
    cp -a "$db" "$SNAP/$name"
    if [[ -n "$CONTAINER" ]]; then
      log "WARN: snapshot of $rel is a plain copy while the hub is running (may catch a mid-write state)"
    else
      log "sqlite snapshot (hub down, plain copy): $rel"
    fi
  fi
done < <(find "$HUB_DATA" -type f -name '*.db' -print0)

# move snapshots into the stage tree (skip when there were none)
shopt -s nullglob
if compgen -G "$SNAP/*" > /dev/null; then
  mv "$SNAP" "$STAGE/hub-snapshots"
fi
shopt -u nullglob

# ── breeze voices (model weights are re-downloadable; see docs/BACKUPS.md) ─
if [[ -d "$BREEZE_DIR/data/voices" ]]; then
  cp -a "$BREEZE_DIR/data/voices" "$STAGE/breeze-voices"
fi

# ── compose/env files (small; .env holds secrets) ──────────────────────────
copy_if() { [[ -f "$1" ]] && { cp -a "$1" "$STAGE/$2"; log "included $2"; }; return 0; }
copy_if "$HUB_DIR/compose.yaml"        riko-hub.compose.yaml
copy_if "$HUB_DIR/.env"                riko-hub.env
copy_if "$BREEZE_DIR/compose.yaml"     breeze-tts.compose.yaml
copy_if "$BREEZE_DIR/.env"             breeze-tts.env

# ── archive (tmp lives inside BACKUP_DIR so the final mv is atomic) ────────
archive="$BACKUP_DIR/riko-hub-$TIMESTAMP.tar.zst"
tar -cf "$TMP/backup.tar" -C "$STAGE" .
zstd -q -f "$TMP/backup.tar" -o "$TMP/backup.tar.zst"
mv -f "$TMP/backup.tar.zst" "$archive"
chmod 600 "$archive"
size="$(du -h "$archive" | cut -f1)"
log "wrote $archive ($size)"

# ── rotation: keep the newest $KEEP ────────────────────────────────────────
mapfile -t archives < <(ls -1t "$BACKUP_DIR"/riko-hub-*.tar.zst 2>/dev/null || true)
if (( ${#archives[@]} > KEEP )); then
  for old in "${archives[@]:KEEP}"; do
    rm -f "$old"
    log "rotated out $old"
  done
fi

log "backup_hub.sh finished OK (keep=$KEEP, archives on disk=${#archives[@]})"
exit 0
