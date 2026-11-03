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
  "updated_at": "2026-11-03T18:00:00Z"
}
```

* `id` — lowercase slug of the name (`[a-z0-9-]`), unique, **immutable**. It
  never changes, even if `name` is edited; use it as your cache key.
* `keywords` — extra words that switch to this persona (see *Switching*).
* `greeting` — what a switch replies with (fallback `"<name> here."`).
* `tts_voice` — optional voice name a client may pass to its own TTS
  (empty = client default). The hub never synthesises speech.
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
| `GET /api/settings` | LLM settings + defaults + per-field source (key never returned) |
| `PUT /api/settings` | change settings (blank field = unset → default; `clear_api_key`) |
| `POST /api/settings/reset` | drop saved LLM/switch-phrase overrides → defaults |
| `POST /api/settings/test` | one `/v1/models` call with merged settings → `{ok, model}` |
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

Body: `{"text": "…", "source": "android", "persona": optional id}`.

```json
{
  "reply": "Melon bread, obviously, senpai.",
  "persona": "riko",
  "switched": false,
  "message_ids": [41, 42],
  "state_version": 17
}
```

Two outcomes:

1. **Switch command** (see below): `switched: true`, `reply` is the target's
   greeting, `message_ids: []` — **nothing is stored** and the target becomes
   active (which bumps `state.version`).
2. **Normal turn**: the hub builds `system_prompt + last <max_history>
   stored messages + your text`, calls the LLM, then stores your message and
   the reply (both with your `source`) and returns both ids.

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
  "switch_phrases": ["switch to", "change to", "become", "persona",
                     "talk to", "i want to talk to"],
  "defaults": {"llm": {"base_url": "http://192.0.2.51:8000/v1", "model": "auto",
                       "temperature": 0.8, "max_tokens": 4096, "max_history": 40,
                       "reasoning_effort": "", "extra_body": {}, "api_key_set": true},
               "switch_phrases": ["switch to", "..."]},
  "llm_source": {"base_url": "saved", "model": "default", "api_key": "default",
                 "temperature": "default", "...": "...", "switch_phrases": "default"}
}
```

`defaults.llm` is the built-in default overlaid with the `RIKO_LLM_*` env the
server started with; `llm_source` says per field whether the effective value is
`"saved"` (via PUT) or `"default"`. The API key is **write-only** — only
`api_key_set` is ever returned.

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
- Numbers validate as before: `422` on non-numeric or negative
  temperature/max_tokens/max_history, or non-object `extra_body`.

`POST /api/settings/reset` with `{"llm": true, "switch_phrases": true}` (either
side optional, `422` if neither) drops the saved overrides so the defaults
apply again. Returns the same body as `GET`.

`POST /api/settings/test` costs one `GET /v1/models` against the currently
merged settings and returns `{"ok": true, "model": "<resolved id>"}` or
`{"ok": false, "error": "<reason>"}` (e.g. `"RuntimeError: LLM not
configured"`, auth failures surface here). Never reveals the key.

Changing any LLM setting re-resolves `model: "auto"` on the next chat; the
state `version` bumps on every `PUT` and on a `reset` that actually removed
something.

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
| `400` | malformed JSON body, bad query params, bad avatar file/size, missing `persona` | `{"detail": "…"}` |
| `401` | missing/wrong hub key | `{"detail": "invalid api key"}` |
| `404` | unknown persona id/keyword | `{"detail": "…"}` |
| `409` | persona slug exists / delete of last persona | `{"detail": "…"}` |
| `422` | validation (empty name, bad import rows, non-object extra_body, empty `text`) | `{"detail": "…"}` (FastAPI may return its own validation shape) |
| `502` | LLM call failed — **nothing was stored**, safe to retry | `{"detail": "<ExceptionType>: <reason>"}` |
| `503` | no `llm.base_url` configured | `{"detail": "LLM not configured"}` |

## A minimal Android flow

1. `GET /api/health` → reachable? then `GET /api/state` with the key → cache
   `version`, `active`, `last_message_id`; `GET /api/history?limit=50` to
   paint the transcript; download `active`'s avatar and remember its ETag.
2. Send: `POST /api/chat {text, source:"android"}`. If `switched`, swap the
   UI to the new persona (and its avatar); else append the returned pair.
3. In a coroutine, poll `GET /api/state` every 3 s; on `version` change apply
   the diff rules from *The polling pattern*. Treat `401` as "key wrong — ask
   again" and `502` as "retry later, history is intact".
