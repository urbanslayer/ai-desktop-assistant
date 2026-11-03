# Riko hub HTTP API (v1)

The hub is one small FastAPI server that owns Riko's personas, chat memory and
LLM settings. Every client (desktop, Android, terminal, web page) talks this
API over plain HTTP — there is no other state anywhere.

* **Base URL** — local mode: `http://127.0.0.1:<local_port>`; Docker on the AI
  box: `http://<box>:8002`. Examples below use `HUB=http://192.0.2.51:8002`.
* **Content type** — JSON (`application/json; charset=utf-8`) everywhere,
  except avatar bodies (binary `model/gltf-binary`) and avatar uploads
  (multipart).
* **Timestamps** — ISO-8601 UTC strings with a `Z` suffix, second precision.
* **CORS** — not enabled: the web config page is served by the hub itself.
  Native clients are unaffected.

## Auth

When the hub was started with a key, **every** `/api/*` route except
`GET /api/health` requires:

```
Authorization: Bearer<key>
```

Missing or wrong key → `401 {"detail": "invalid api key"}`. If the hub has no
key configured, send nothing and everything is open. Get the key from your
`.env` (`RIKO_HUB_KEY`) — in the Android app keep it in `BuildConfig`/secure
storage, never in a log.

```bash
curl -s $HUB/api/health                        # always open
curl -s -H "Authorization: Bearer$KEY" $HUB/api/state

# the examples below reuse:
#   AUTH='Authorization: Bearer '"$KEY'
```

## Objects

### Persona

```json
{
  "id": "riko",
  "name": "Riko",
  "keywords": ["riko"],
  "system_prompt": "You are a helpful assistant named Riko…",
  "greeting": "Riko here. Miss me, senpai?",
  "tts_voice": "",
  "avatar_version": 1,
  "has_avatar": true,
  "voice": {
    "mode": "design",              // design | clone | off
    "description": "A proud, sharp teenage girl…",
    "style": "",                   // optional delivery direction
    "ref_text": "",                // transcript of the reference clip (clone)
    "has_reference": false,        // read-only: is a clip stored on the hub?
    "seed": 823145,                // fixed => the designed voice is reproducible
    "cfg_scale": null              // null = breeze server default
  },
  "voice_version": 1,              // +1 on every voice/reference/lock change
  "updated_at": "2026-11-03T18:00:00Z"
}
```

* `id` — lowercase slug of the name (`[a-z0-9-]`), unique, **immutable**. It
  never changes, even if `name` is edited; use it as your cache key.
* `keywords` — extra words that switch to this persona (see *Switching*).
* `greeting` — what a switch replies with (fallback `"<name> here."`).
* `tts_voice` — legacy client-side voice name, kept for old clients; the
  hub-side voice is `voice` (see *Voice & audio*). The hub synthesises all
  speech via Breeze-TTS-2.
* `voice` / `voice_version` — per-persona Breeze voice. `voice_version` is
  the **audio cache key**: fetch audio URLs with `?v=<voice_version>` and
  cache the bytes by `(persona, v)`; when it grows, old audio is stale.
* `avatar_version` — integer, +1 on every avatar upload. **Cache avatars by
  `id` + `avatar_version`** (see *Avatars*).

### Message

```json
{
  "id": 42,
  "persona": "riko",
  "role": "user",
  "content": "hi",
  "source": "android",
  "created_at": "2026-11-03T18:00:00Z"
}
```

`id` is global and monotonically increasing across all personas — it is your
`after` cursor. `role` is `user` or `assistant`. `source` is the client name
you passed to `/api/chat` (`desktop`, `android`, `web`, `terminal`,
`import`, …).

### State

```json
{
  "version": 17,
  "active": { …full Persona… },
  "personas": [{"id": "riko", "name": "Riko"}, {"id": "asuka", "name": "Asuka"}],
  "last_message_id": 42,
  "llm_model": "qwen4exp"
}
```

`version` increments on **any** change: switch, persona create/edit/delete,
avatar upload, new message, history cleared, settings change. `llm_model` is
the currently believed model id (for `model: "auto"` it appears after the
first successful chat).

## The polling pattern (how clients stay in sync)

Every client owns writes; nobody owns reads. Poll `GET /api/state` every few
seconds (the desktop app uses 3 s) and diff:

```kotlin
// pseudo-Kotlin loop, every 3s on a background coroutine
val s = hub.state()
if (s.version != last.version) {
    if (s.active.id != last.active.id) reloadPersona(s.active)      // prompt + voice
    if (s.active.avatarVersion > last.active.avatarVersion) refetchAvatar(s.active)
    if (s.lastMessageId > last.lastMessageId) {
        val delta = hub.history(persona = s.active.id, after = last.lastMessageId)
        appendToChat(delta.messages)                                 // no re-fetch of everything
    }
    last = s
}
```

Messages never mutate or are deleted individually — the only bulk operation
is `DELETE /api/history`, which resets a persona's history (your cursor may
then jump *backwards*; if `lastMessageId < last.lastMessageId`, refetch the
transcript with `after=0`).

## Routes

| method & path | purpose |
|---|---|
| `GET /api/health` | liveness (no auth) |
| `GET /api/state` | everything a client needs to detect changes |
| `GET /api/personas` | list personas |
| `POST /api/personas` | create |
| `GET /api/personas/{id}` | fetch one |
| `PUT /api/personas/{id}` | edit |
| `DELETE /api/personas/{id}` | delete (+ its messages and avatar) |
| `GET /api/personas/{id}/avatar` | download `.vrm` |
| `PUT /api/personas/{id}/avatar` | upload `.vrm` (multipart) |
| `POST /api/active` | switch active persona |
| `GET /api/history` | read transcript (paged by id) |
| `DELETE /api/history` | clear a persona's transcript |
| `POST /api/history/import` | bulk-import old logs |
| `POST /api/chat` | the only writing route in daily use |
| `GET /api/messages/{id}/audio` | cached WAV of an assistant reply |
| `GET /api/personas/{id}/greeting/audio` | cached WAV of a greeting |
| `POST /api/tts` | one-shot WAV (never cached) for a client line |
| `GET /api/tts/status` | `{enabled, reachable, base_url, voices}` |
| `PUT /api/personas/{id}/voice` | edit voice (mode/description/style/seed/cfg_scale) |
| `PUT /api/personas/{id}/voice/reference` | upload reference clip (multipart) → clone |
| `GET /api/personas/{id}/voice/reference` | download the stored clip |
| `DELETE /api/personas/{id}/voice/reference` | drop clip → back to design |
| `POST /api/personas/{id}/voice/preview` | uncached WAV in this voice |
| `POST /api/personas/{id}/voice/lock` | freeze designed voice: its take becomes the clone |
| `GET /api/settings` | LLM + TTS settings + defaults + per-field source (key never returned) |
| `PUT /api/settings` | change settings (blank field = unset → default; `clear_api_key`) |
| `POST /api/settings/reset` | drop saved LLM/TTS/switch-phrase overrides → defaults |
| `POST /api/settings/test` | model probe + breeze health → `{ok, model, tts}` |
| `GET /` | web config page (no auth; asks for the key) |

### GET /api/health

```bash
curl -s $HUB/api/health
# {"ok": true, "version": "1"}
```

`version` is the API major version. No auth. Use it to detect "hub is up"
before authenticating.

### GET /api/state

Returns the State object. Cheap — poll it freely.

```bash
curl -s -H "$AUTH" $HUB/api/state
```

### GET /api/personas

```bash
curl -s -H "$AUTH" $HUB/api/personas      # -> [Persona, …] in creation order
```

### POST /api/personas → `201`

Body: `{name, system_prompt, keywords?, greeting?, tts_voice?}`
(`system_prompt` defaults to `""`; keywords default to `[slug(name)]` plus
whatever you pass; keywords are lower-cased by the hub).

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Asuka","system_prompt":"You are Asuka, a tsundere…","keywords":["asuka","td"],"greeting":"Hmph! What is it, senpai?"}' \
  $HUB/api/personas
```

Errors: `409` the slug already exists; `422` name empty (or no slug possible);
`400` invalid JSON body.

### GET /api/personas/{id} · PUT /api/personas/{id}

`PUT` takes any subset of `{name, system_prompt, keywords, greeting,
tts_voice}` — everything else stays. The `id` never changes. Returns the full
updated Persona; `404` unknown id.

### DELETE /api/personas/{id} → `204`

Deletes the persona **with its messages and uploaded avatar**. `409` if it is
the last persona (the hub always has ≥1). If you delete the active persona,
the first remaining one becomes active.

### GET /api/personas/{id}/avatar

Streams the persona's `.vrm` file: `Content-Type: model/gltf-binary`,
`ETag: "<id>-<avatar_version>"`. With no upload on that persona you get the
built-in default avatar (still correctly ETagged, `avatar_version` 0).
Send `If-None-Match: "<id>-<avatar_version>"` and you get `304` with no body.

**Client caching rule:** store the file next to a record of
`(id, avatar_version)` (or just rely on ETag revalidation). When
`state.active.avatar_version` grows, re-download.

```bash
curl -s -o asuka.vrm -D - -H "$AUTH" $HUB/api/personas/asuka/avatar
# ETag: "asuka-2"   …later:
curl -s -H "$AUTH" -H 'If-None-Match: "asuka-2"' -o /dev/null -w '%{http_code}\n' \
  $HUB/api/personas/asuka/avatar     # -> 304
```

### PUT /api/personas/{id}/avatar

Multipart field name is `file`. Max 200 MB; the bytes must start with the
glTF magic (`glTF`) or you get `400`. On success the Persona comes back with
`avatar_version` +1 (and `state.version` has moved).

```bash
curl -s -X PUT -H "$AUTH" -F 'file=@asuka.vrm;type=model/gltf-binary' \
  $HUB/api/personas/asuka/avatar
```

### POST /api/active

Body `{"persona": "<id, name or keyword>"}` — case-insensitive, surrounding
`!?.` tolerated. Returns the new State; `404` when nothing matches. Switching
to the already-active persona returns `200` with no `version` bump.

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"persona":"asuka"}' $HUB/api/active
```

### GET /api/history

Query params: `persona` (default: active), `after` (message id, default 0),
`limit` (default 50, max 500 — larger values are clamped).

```json
{"persona": "asuka", "messages": [ …oldest → newest… ]}
```

**Semantics:** `after=0` → the **last** `limit` messages (fetch the tail);
`after=<id>` → messages with `id > after` (fetch a delta). Unknown persona →
`404`; non-integer `after`/`limit` → `400`.

```bash
curl -s -H "$AUTH" "$HUB/api/history?limit=200"                   # tail of active persona
curl -s -H "$AUTH" "$HUB/api/history?persona=asuka&after=42"      # everything after id 42
```

### DELETE /api/history → `204`

Clears the transcript of `?persona=` (default active). Bumps `state.version`.

### POST /api/history/import

Body: `{"persona": id, "messages": [{"role","content"}…], "replace": false}`.
Appends (or with `replace: true`, clears then appends) with `source:
"import"`; returns `{"imported": n}`. Used once to migrate an old
`chat_history.json`. `422` on a bad role/content, `404` on unknown persona.

### POST /api/chat → ChatResult

Body: `{"text": "…", "source": "android", "persona": optional id,
"speak": optional bool (default true)}`.

```json
{
  "reply": "Melon bread, obviously, senpai.",
  "persona": "riko",
  "switched": false,
  "message_ids": [41, 42],
  "audio_url": "/api/messages/42/audio",
  "state_version": 17
}
```

Two outcomes:

1. **Switch command** (see below): `switched: true`, `reply` is the target's
   greeting, `message_ids: []` — **nothing is stored** and the target becomes
   active (which bumps `state.version`). `audio_url` points at the greeting
   WAV: `/api/personas/<id>/greeting/audio?v=<voice_version>`.
2. **Normal turn**: the hub builds `system_prompt + last <max_history>
   stored messages + your text`, calls the LLM, then stores your message and
   the reply (both with your `source`) and returns both ids. `audio_url` is
   `/api/messages/<assistant_id>/audio`.

`audio_url` is `null` when `speak: false`, when TTS is off/unconfigured, or
when that persona's `voice.mode` is `off`. **The chat never waits for
speech** — synthesis happens lazily on the first audio fetch (see *Voice &
audio*), so fetch it only if you actually play it.

`persona` (optional) answers with that persona **without** switching the
active one. Errors: `422` empty `text`; `404` unknown persona; `503 {"detail":
"LLM not configured"}`; `502 {"detail": "<reason>"}` when the LLM call fails —
**in that case nothing was stored** and your send can be retried safely.
Chats are serialised hub-side (one LLM turn at a time); concurrent requests
queue, so set a generous client timeout (the LLM may think for up to a
minute).

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"hi","source":"android"}' $HUB/api/chat
```

### GET /api/settings · PUT /api/settings · POST /api/settings/reset · POST /api/settings/test

`GET` returns the **merged** settings plus where each value came from:

```json
{
  "llm": {"base_url": "http://192.0.2.51:8000/v1", "model": "auto",
          "temperature": 0.8, "max_tokens": 4096, "max_history": 40,
          "reasoning_effort": "", "extra_body": {}, "api_key_set": true},
  "tts": {"enabled": true, "base_url": "http://breeze-tts:8137",
          "timeout": 120, "parallel_note": ""},
  "switch_phrases": ["switch to", "change to", "become", "persona",
                     "talk to", "i want to talk to"],
  "defaults": {"llm": {"base_url": "http://192.0.2.51:8000/v1", "model": "auto",
                       "temperature": 0.8, "max_tokens": 4096, "max_history": 40,
                       "reasoning_effort": "", "extra_body": {}, "api_key_set": true},
               "tts": {"enabled": true, "base_url": "http://breeze-tts:8137",
                       "timeout": 120, "parallel_note": ""},
               "switch_phrases": ["switch to", "..."]},
  "llm_source": {"base_url": "saved", "model": "default", "api_key": "default",
                 "temperature": "default", "...": "...", "switch_phrases": "default"},
  "tts_source": {"base_url": "default", "enabled": "default", "...": "..."}
}
```

`defaults.llm` is the built-in default overlaid with the `RIKO_LLM_*` env the
server started with; `defaults.tts` likewise with `RIKO_TTS_BASE_URL` /
`RIKO_TTS_ENABLED`; `llm_source` / `tts_source` say per field whether the
effective value is `"saved"` (via PUT) or `"default"`. The API key is
**write-only** — only `api_key_set` is ever returned.

`PUT` takes a partial body — send only what you changed:

```bash
curl -s -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"llm":{"temperature":0.7,"api_key":"sk-secret"}}' $HUB/api/settings
```

Semantics (made so a careless form save cannot break the hub):

- **Blank = unset, never an override.** Sending `""` (or whitespace) for
  `base_url`, `model` or `reasoning_effort` **deletes the saved override**, so
  the default applies again (`model` then resolves to `RIKO_LLM_MODEL` or
  `"auto"`). A blank `base_url` can therefore never shadow the env default.
- **`llm.api_key` only changes on a non-empty value.** An empty or absent
  `api_key` leaves the stored key untouched (no accidental wipe, no
  autofill-induced override). `"clear_api_key": true` inside `llm` removes the
  stored key so the `RIKO_LLM_API_KEY` env default applies again — there is no
  way to force a keyless LLM over an env default (not needed).
- **`switch_phrases: []` resets to the defaults**, it does not disable phrase
  switching (a persona's keywords and name still switch on their own either
  way). Any non-empty list replaces the defaults.
- **The `tts` block follows the same rules**: blank `base_url` deletes the
  saved override so `RIKO_TTS_BASE_URL` applies again (an empty default
  means voice is off everywhere); `enabled` is a plain boolean; `timeout` ≥ 1;
  only `base_url` and `parallel_note` blank back to defaults. Voice is on
  only when `enabled` is true **and** a base URL resolves.
- Numbers validate as before: `422` on non-numeric or negative
  temperature/max_tokens/max_history, or non-object `extra_body`.

`POST /api/settings/reset` with `{"llm": true, "tts": true,
"switch_phrases": true}` (each side optional, `422` if none) drops the saved
overrides so the defaults apply again. Returns the same body as `GET`.

`POST /api/settings/test` costs one `GET /v1/models` against the currently
merged settings plus one breeze `GET /health`, and returns
`{"ok": true, "model": "<resolved id>", "tts": {"ok": true}}` or
`{"ok": false, "error": "<reason>", "tts": {"ok": false, "error": "<reason>"}}`
(e.g. `"RuntimeError: LLM not configured"`, `"TTS not configured"`, `"TTS
disabled in settings"`; auth failures surface here). Never reveals the key.

Changing any LLM setting re-resolves `model: "auto"` on the next chat; the
state `version` bumps on every `PUT` and on a `reset` that actually removed
something.

## Voice & audio (docs/VOICE.md)

The **hub is the only component that talks to Breeze-TTS-2** (the GPU-side
TTS server). Clients never speak TTS dialect: they fetch the hub's audio
URLs and always receive properly-headed 24 kHz mono WAV bytes. Breeze
speaks **English and Chinese only**; its weights are **non-commercial**.

**Fetch rule:** when `audio_url` comes back non-null, `GET` it once (with
the hub key), cache the bytes keyed by the URL, and play locally. The first
fetch of a URL blocks while the hub synthesises once (can take seconds on
GPU contention — do it off the UI thread); every later fetch is a cached
file read. Include `?v=<voice_version>` when you have it — the hub ignores
the value but you get free cache-busting when her voice changes. `409`
means this persona's voice is `off`; `503` means hub-wide TTS is off and
nothing was synthesised.

### GET /api/messages/{id}/audio

The WAV of one **assistant** message in its persona's voice. `404` for user
messages / unknown ids. Cache key server-side is `(message_id,
voice_version)`: editing the persona voice re-synthesises on the next
fetch; old replies keep their old audio until asked again.

### GET /api/personas/{id}/greeting/audio

Same, for the greeting (`?v=` informational). This is what a switch's
`audio_url` points at.

### POST /api/tts

One-shot WAV for a line that is not in any transcript (client-side
narration): body `{"text": "…", "persona": optional id}`, ≤ **1000
characters** (`422`), never cached, `Content-Type: audio/wav`.

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Hmph!","persona":"asuka"}' $HUB/api/tts -o line.wav
```

### GET /api/tts/status

`{"enabled": bool, "reachable": bool, "base_url": "…", "voices": n}` — the
web page's red/green dot. Cheap; `reachable` costs one breeze `/health`.

### PUT /api/personas/{id}/voice

Partial body over `{mode, description, style, ref_text, seed, cfg_scale}` —
`has_reference` is read-only, other unknown fields `422`. `mode`:
`design` (speak from `description` + fixed `seed`), `clone` (needs a stored
reference clip, else `422`), `off` (this persona never gets audio). Only a
**real change** bumps `voice_version` and `state.version`. `cfg_scale`:
number in `(0, 10]` or `null` = breeze default.

### PUT /api/personas/{id}/voice/reference

Multipart `file` + form field `ref_text` (**the clip's exact transcript**,
required). wav/flac/ogg (mp3 when the hub's libsndfile supports it),
**3–30 s**, ≤ 20 MB — downmixed to mono WAV and stored permanently as the
persona's voice source. The hub registers it on breeze **before** touching
anything: a breeze error leaves the persona exactly as it was. On success
the Persona comes back with `voice.mode: "clone"`, the new `ref_text`, and
`voice_version` +1.

```bash
curl -s -X PUT -H "$AUTH" -F 'file=@clip.wav' -F 'ref_text=Exactly what she says.' \
  $HUB/api/personas/asuka/voice/reference
```

`GET` the same path downloads the stored clip (`404` when none); `DELETE`
drops it and returns the persona to `design` mode (`voice_version` +1).

### POST /api/personas/{id}/voice/preview

`{"text": optional}` — one uncached WAV in this persona's current voice
(default text: the greeting). Works even when `mode` is `off`, so you can
audition before enabling. Errors like `/api/tts`.

### POST /api/personas/{id}/voice/lock

`{"text": optional}` — **freeze a designed voice**: the hub says `text`
(default the greeting) once in the current design, stores that take as the
reference clip (`ref_text` = the text), registers it, and switches the
persona to `clone`. `409` unless `mode` is `design`. Do this once per
persona when the user is happy with the voice — clones are what survive
breeze restarts (the hub re-registers from the stored clip on demand).

**Breeze hiccups are the hub's problem, not yours:** a voice that vanished
from breeze (restart) is silently re-registered from the stored clip and
retried once; "all slots busy" (409 from breeze) is retried with backoff.
Persistent failures surface as `502 {"detail": …}` on audio routes, and an
unreachable breeze as `503`.

### Voice cache

Cached WAVs live hub-side under `data/audio/` capped at 500 MB (oldest
evicted). Deleting a persona or clearing its history deletes its cached
audio and stored clip with it.

## Switching semantics (what `POST /api/chat` intercepts)

Before touching the LLM the hub normalises `text`: lower-case, trim, strip
trailing `.!?,`. The message is a **switch command** when either:

1. it equals a persona's `keyword`, `name` (lower-cased) or `id` exactly —
   `"Asuka!"` → `asuka`; or
2. it matches `^(<switch phrase>)\s+(.+)$` and group 2 (normalised the same
   way) equals a persona's keyword/name/id — `"switch to asuka"`,
   `"I want to talk to Asuka?"`.

Switch phrases are configurable (`PUT /api/settings`, `switch_phrases`; an
empty list resets them to the defaults — they cannot be switched off). On a
switch nothing is written to history, so persona changes are invisible in the
transcript; each persona has its own isolated memory. Anything else is a
normal chat turn — `"asuka is loud, but tell me a joke"` does **not** switch.
Switching to the already active persona still returns `switched: true` (so a
client can replay the greeting) but leaves `state.version` alone.

## Error codes

| code | meaning | body |
|---|---|---|
| `400` | malformed JSON body, bad query params, bad avatar/reference file or size, missing `ref_text` | `{"detail": "…"}` |
| `401` | missing/wrong hub key | `{"detail": "invalid api key"}` |
| `404` | unknown persona id/keyword, unknown message id, missing reference clip | `{"detail": "…"}` |
| `409` | persona slug exists / delete of last persona / voice lock on a non-design voice / audio for a persona with `voice.mode: "off"` | `{"detail": "…"}` |
| `422` | validation (empty name, bad import rows, non-object extra_body, empty `text`, over-long `/api/tts` text, bad voice/tts field values) | `{"detail": "…"}` (FastAPI may return its own validation shape) |
| `502` | LLM call failed — **nothing was stored**, safe to retry. Also: breeze synthesis/registration failed | `{"detail": "<ExceptionType>: <reason>"}` |
| `503` | no `llm.base_url` configured; or TTS off/unreachable on an audio route | `{"detail": "LLM not configured"}` / `{"detail": "TTS …"}` |

## A minimal Android flow

1. `GET /api/health` → reachable? then `GET /api/state` with the key → cache
   `version`, `active`, `last_message_id`; `GET /api/history?limit=50` to
   paint the transcript; download `active`'s avatar and remember its ETag.
2. Send: `POST /api/chat {text, source:"android"}`. If `switched`, swap the
   UI to the new persona (and its avatar); else append the returned pair.
   If `audio_url` came back, fetch it in a background coroutine (first fetch
   can take seconds — it synthesises once), cache the WAV by URL, and play.
3. In a coroutine, poll `GET /api/state` every 3 s; on `version` change apply
   the diff rules from *The polling pattern*. Treat `401` as "key wrong — ask
   again" and `502` as "retry later, history is intact".
