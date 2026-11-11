# Config: the file, the local override, and who writes what

## The one loader

`riko/config.py:load_config()` is the ONLY place the app reads
`character_config.yaml`. Search order for the base file:

1. the explicit `path` argument,
2. `$RIKO_CONFIG` (alternate yaml for tests / nested smoke runs),
3. `character_config.yaml` next to the repo root.

`desktop.py`, `main.py`, `riko/asr.py`, `riko/tts.py`, `riko/hub_client.py`
and `riko/migrate.py` all go through it — there are no ad-hoc readers.
(`bin/riko-ctl` talks HTTP to the running app; it never parses the yaml.)

## Local overrides (`character_config.local.yaml`)

The tracked `character_config.yaml` ships **example** addresses from the RFC
5737 documentation range (`192.0.2.51`): good defaults to read, never your
real LAN. Put your real ones in `character_config.local.yaml` next to it —
gitignored, private, yours. Copy `character_config.local.example.yaml` to
start; the usual overrides are `hub.url`, `llm.base_url`, `remote_asr.url`
and `openai_tts.url`.

Merge rules (applied by `load_config`, implemented in `deep_merge`):

- **dicts merge recursively**, key by key, local wins on collisions;
- **lists and scalars replace wholesale** (no element-wise list merging);
- a missing local file is not an error — the base file is used as-is;
- a malformed local file stops startup with a message that names the file
  (same for a malformed base file);
- the rule follows the base file: if `$RIKO_CONFIG` points somewhere else,
  the loader looks for `<that-stem>.local.yaml` **next to that file**, so
  smoke tests with a temp config get their own optional sibling.

```yaml
# character_config.local.yaml — only what differs from the shipped example
hub:
  url: http://192.0.2.51:8002      # your real hub box
llm:
  base_url: http://192.0.2.51:8000/v1
remote_asr:
  url: http://192.0.2.51:8001/v1
openai_tts:
  url: http://192.0.2.51:8880/v1
```

## Who writes what

Nothing in the app ever writes `character_config.yaml` (or the local file):
the config is read-only at runtime. Every toggle that "persists" a choice —
listen mode, desktop awareness, per-persona avatar scale, name gate, song
ticker — lives in the appstate JSON files under `~/.config/riko/`
(`riko/appstate.py`, `$RIKO_APP_STATE_DIR`/`$RIKO_CONFIG_DIR` in tests) and
overrides the config's seed value from then on. A value set in
`character_config.local.yaml` therefore can never leak back into the base
file: there is no write path to leak through.

## Backup pull

`scripts/backup_pull.sh` deliberately has no personal default remote:
set `RIKO_BACKUP_REMOTE` (what the systemd unit should provide) or
`RIKO_BACKUP_HOST` + `RIKO_BACKUP_USER`. See docs/BACKUPS.md.
