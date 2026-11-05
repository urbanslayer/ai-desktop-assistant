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
  "emotes": true,                  // emotion tags on/off (see *Emotion tags*)
  "appearance": "long white-and-blue hair, blue eyes, a blue tailcoat…",
                                   // how she looks (≤ 1000 chars, see *Body actions*)
  "embodied": true,                // may act with the 3D body (see *Body actions*)
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
* `emotes` — when true the hub instructs the model to open every reply with
  an emotion tag and hands you the parsed emotion as structured metadata (see
  *Emotion tags*). Default **true**, including for personas that existed
  before this field existed.
* `appearance` — free text (≤ 1000 chars, `422` above that) describing how
  she looks; when set the hub appends `Your appearance: …` to the system
  prompt so she can describe herself, **even in text-only chats with no body
  online** (see *Body actions*). Default `""`.
* `embodied` — when false the body block is **never** added for this persona
  and switch greetings never auto-wave, even with an embodied client online
  (see *Body actions*). Default **true**, including for existing personas.

### Message

```json
{
  "id": 42,
  "persona": "riko",
  "role": "user",
  "content": "hi",
  "source": "android",
  "emotion": "teasing",            // assistant messages only, else null
  "actions": [{"name": "wave", "at": 0.0}],  // assistant messages; [] unless the turn acted
  "tools_used": [                  // [] unless the turn ran tools
    {"tool": "web_search", "label": "searching the web for “tokyo weather”"}
  ],
  "memory_writes": [               // [] unless the turn saved facts (memory round M)
    {"id": 7, "text": "User's cat is called Miso"}
  ],
  "voice": false,                  // user messages: true when the text came from speech recognition
  "timer_id": null,                // set on BOTH messages of a fired timer turn (docs/TIMERS.md)
  "created_at": "2026-11-03T18:00:00Z"
}
```

`id` is global and monotonically increasing across all personas — it is your
`after` cursor. `role` is `user` or `assistant`. `source` is the client name
you passed to `/api/chat` (`desktop`, `android`, `web`, `terminal`,
`import`, …). `emotion` is one of the fixed emotion ids (see *Emotion tags*)
and `content` **never contains the raw tag**; `actions` is the parsed
body-action list (see *Body actions*, where an action may carry the additive
`"asked": true` flag) and `content` never contains the raw
action tags either; `tools_used` is the compact trace of which tools the turn
called (full history in `/api/tools/log`); `memory_writes` lists the long-term
facts the turn saved via `remember` — render it as a "remembered" chip (see
*Memory*).

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
avatar upload, new message, history cleared, settings change, memory add/edit/
forget. `llm_model` is
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
| `GET /api/memory` | list/search long-term facts (scope filter) |
| `POST /api/memory` | add a fact by hand (dedups) |
| `PUT /api/memory/{id}` | edit a fact's text / pin |
| `DELETE /api/memory/{id}` | forget one fact |
| `DELETE /api/memory` | forget everything (`confirm=yes`; optional `scope`) |
| `GET /api/memory/export` | all facts as JSON |
| `POST /api/memory/import` | restore exported facts (`replace?`) |
| `GET /api/events` | long-poll live events (turn/tool progress, approvals) |
| `GET /api/tools` | built-in + online client tools, permissions, settings, status |
| `GET /api/tools/log` | recent tool-call log entries |
| `GET /api/tools/status` | function-calling health (`ok` / `rejected` / `unknown`) |
| `GET /api/clients` | online client-tool registrations |
| `POST /api/clients/{id}/register` | register this client's runnable tools |
| `POST /api/clients/{id}/heartbeat` | keep-alive (~every 30 s) |
| `GET /api/clients/{id}/requests` | long-poll for tool calls to run |
| `POST /api/clients/{id}/results` | return a tool call's result/approval |
| `DELETE /api/clients/{id}` | unregister |
| `GET /api/messages/{id}/audio` | cached WAV of an assistant reply |
| `GET /api/personas/{id}/greeting/audio` | cached WAV of a greeting |
| `POST /api/tts` | one-shot WAV (never cached) for a client line |
| `GET /api/tts/status` | `{enabled, reachable, base_url, voices}` |
| `PUT /api/personas/{id}/voice` | edit voice (mode/description/style/seed/cfg_scale) |
| `PUT /api/personas/{id}/voice/reference` | upload reference clip (multipart) → clone |
| `GET /api/personas/{id}/voice/reference` | download the stored clip |
| `DELETE /api/personas/{id}/voice/reference` | drop clip → back to design |
| `POST /api/personas/{id}/voice/preview` | uncached WAV in this voice (`text`/`voice` overrides) |
| `POST /api/personas/{id}/voice/lock` | freeze designed voice: its take becomes the clone |
| `GET /api/settings` | LLM + TTS + tools settings + defaults + per-field source (key never returned) |
| `PUT /api/settings` | change settings (blank field = unset → default; `clear_api_key`) |
| `POST /api/settings/reset` | drop saved LLM/TTS/tools/memory/MCP/timers/timezone/switch-phrase overrides → defaults |
| `POST /api/settings/test` | model probe + breeze health → `{ok, model, tts}` |
| `GET /api/timers` | one-shot timers (docs/TIMERS.md; `?status=` filter) |
| `DELETE /api/timers/{id}` | cancel a pending/firing timer |
| `POST /api/chat/read` | "owner read this persona's chat" → cancels `chat.read` timers |
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

Body: `{name, system_prompt, keywords?, greeting?, tts_voice?, emotes?}`
(`system_prompt` defaults to `""`; keywords default to `[slug(name)]` plus
whatever you pass; keywords are lower-cased by the hub; `emotes` defaults to
`true`).

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Asuka","system_prompt":"You are Asuka, a tsundere…","keywords":["asuka","td"],"greeting":"Hmph! What is it, senpai?"}' \
  $HUB/api/personas
```

Errors: `409` the slug already exists; `422` name empty (or no slug possible);
`400` invalid JSON body.

### GET /api/personas/{id} · PUT /api/personas/{id}

`PUT` takes any subset of `{name, system_prompt, keywords, greeting,
tts_voice, emotes}` — everything else stays. The `id` never changes. Returns
the full updated Persona; `404` unknown id.

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

Messages carry the same `speech` field as ChatResults (assistant rows: the
cleaned spoken text, never a URL — filled on read for rows stored before the
spoken-text round; `""` on user rows). `content` stays the full display text.

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
"speak": optional bool (default true), "voice": optional bool (default
false)}`. `voice: true` says the text came off speech recognition: the hub
adds one line to the system prompt telling her names may be misheard
(e.g. *Farina* for *Furina*, with the hub's persona names listed) and that
she must silently assume the closest name instead of commenting, and the
stored user message carries `voice: true` (🎤 on the web page). Clients on
older hubs may send it anyway — the field is simply ignored there.

```json
{
  "reply": "It is 21 C and sunny in Tokyo today, senpai.",
  "speech": "It is 21 C and sunny in Tokyo today, senpai.",
  "persona": "riko",
  "switched": false,
  "emotion": "happy",
  "actions": [{"name": "wave", "at": 0.0}],
  "tools_used": [{"tool": "web_search", "label": "searching the web for “tokyo weather”"}],
  "memory_writes": [{"id": 7, "text": "User's cat is called Miso"}],
  "timers_set": [{"id": 3, "label": "stretch",
                  "due_at_local": "2026-11-05T12:30:00+00:00", "asked": false}],
  "message_ids": [41, 42],
  "audio_url": "/api/messages/42/audio",
  "state_version": 17
}
```

Two outcomes:

1. **Switch command** (see below): `switched: true`, `reply` is the target's
   greeting, `message_ids: []`, `emotion: null`, `tools_used: []` — **nothing
   is stored** and the target becomes active (which bumps `state.version`).
   `speech` is that greeting's spoken text (see below).
   `audio_url` points at the greeting WAV:
   `/api/personas/<id>/greeting/audio?v=<voice_version>`.
   `actions` is `[{"name": "wave", "at": 0.0}]` when the target persona is
   `embodied` and the online body set has `wave` (see *Body actions*), else
   `[]`.
2. **Normal turn**: the hub builds `system_prompt + emotion/body/appearance
   prompt lines (+ the speech-recognition line when `voice: true`) + last
   <max_history> stored messages + your text`, runs the
   model (possibly through the tool loop — see *Tools*), strips any emotion
   tag and action tags, then stores your message and the clean reply (both
   with your `source`, the assistant one with `emotion` + `actions` +
   `tools_used`) and returns both ids. `audio_url` is
   `/api/messages/<assistant_id>/audio`.

`emotion` is the parsed emotion id (`neutral` when the persona has `emotes`
off or the model gave no/unknown tag) — drive your VRM expression from it,
the raw `[tag]` is **never** in `reply` (see *Emotion tags*). `actions` is
**always present**, `[]` when the turn did not act; on tool-loop turns it
comes from the **final** reply only (see *Body actions*). `tools_used` is
`[]` when no tools ran; `memory_writes` is likewise **always present** and
lists the long-term facts this turn saved with `remember`
(`[{id, text}]`) — render it as a "remembered" chip on the reply (see
*Memory*). `timers_set` is likewise **always present** and lists the timers
this turn created (`[{id, label, due_at_local, asked}]`, see *Timers*):
`asked: true` marks the hub's asked-for fallback timer — created because the
owner explicitly asked for one and the model answered without calling the
tool. A switch's ChatResult carries `timers_set: []`. While a turn runs you
can watch live progress on
`GET /api/events` (see *Events*). A turn that uses tools can take noticeably
longer than a plain one (each tool step is another model call): keep your
chat timeout generous (minutes, not seconds).

`speech` is what she **says**; `reply` is what she **shows**. The hub
synthesizes its audio from `speech`, not `reply`: URLs dropped (markdown
links keep only their label), bare website names and email addresses dropped
(`docs.godotengine.org`, `you@example.com` — while version numbers like
`3.10.2`, file names like `config.yaml`, abbreviations like `e.g.`, and tech
names like `Node.js`/`ASP.NET` stay), code blocks and inline code dropped,
markdown furniture and emoji dropped, source citations like `[1]` dropped.
**Every prose paragraph is spoken** — a multi-paragraph answer is spoken in
full, not just its first paragraph. What gets dropped instead are the
details blocks, wherever they sit: paragraphs that are only links or link
intros ("Docs at X:"), bullet/numbered lists of links, "Sources:"/"Links:"
sections, markdown tables and images. Prose bullet lists ARE spoken (markers
removed). The whole spoken text is capped at **2500 characters**, cut at a
sentence end (Breeze splits long text itself on sentence boundaries —
`split_chars`, default 600, docs/VOICE.md and `hub/voice.py` — so the hub
sends one request; the cap is there because a wall of prose is not a spoken
answer (docs/TOOLS.md A: links and sources go after a blank line, they are
shown, not spoken).
A reply whose paragraphs were all links/details speaks a short fallback line
("I put the link on screen."). The rule set is versioned (the hub's audio
cache keys on it); clients that run their own TTS (`tts: openai/sovits/fish`)
must speak `speech` — never `reply` — or apply the identical cleaner
(`riko/speech.py`). Display `reply` in full: links stay visible.

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
  "tools": {"enabled": true, "max_steps": 8, "max_seconds": 120,
            "max_result_chars": 12000, "searxng_url": "http://192.0.2.101:8989",
            "allow_private_fetch": false, "permissions": {"desktop__do_thing": "never"}},
  "mcp": {"servers": [{"name": "time", "transport": "stdio", "enabled": true,
                       "command": "uvx", "args": ["mcp-server-time"], "env": {},
                       "tool_permissions": {}, "timeout": 60}]},
  "memory": {"enabled": true, "prompt_budget_tokens": 600,
             "auto_remember": true},
  "timers": {"allow_chaining": false},              // docs/TIMERS.md reviewer additions
  "switch_phrases": ["switch to", "change to", "become", "persona",
                     "talk to", "i want to talk to"],
  "timezone": "",                                  // saved override, "" = server zone
  "timezone_source": "default",                    // docs/TIMERS.md: timers + spoken times
  "defaults": {"llm": {"base_url": "http://192.0.2.51:8000/v1", "model": "auto",
                       "temperature": 0.8, "max_tokens": 4096, "max_history": 40,
                       "reasoning_effort": "", "extra_body": {}, "api_key_set": true},
               "tts": {"enabled": true, "base_url": "http://breeze-tts:8137",
                       "timeout": 120, "parallel_note": ""},
               "tools": {"enabled": true, "max_steps": 8, "max_seconds": 120,
                         "max_result_chars": 12000, "searxng_url": "",
                         "allow_private_fetch": false, "permissions": {}},
               "mcp": {"servers": []},
               "memory": {"enabled": true, "prompt_budget_tokens": 600,
                          "auto_remember": true},
               "timers": {"allow_chaining": false},
               "switch_phrases": ["switch to", "..."],
               "timezone": "Europe/London"},
  "llm_source": {"base_url": "saved", "model": "default", "api_key": "default",
                 "temperature": "default", "...": "...", "switch_phrases": "default"},
  "tts_source": {"base_url": "default", "enabled": "default", "...": "..."},
  "tools_source": {"enabled": "default", "searxng_url": "saved", "...": "..."},
  "mcp_source": {"servers": "saved"},
  "memory_source": {"enabled": "default", "prompt_budget_tokens": "default",
                    "auto_remember": "saved"},
  "timers_source": {"allow_chaining": "saved"}
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
- **The `tools` block** (see *Tools*): blank `searxng_url` deletes the saved
  override so `RIKO_SEARXNG_URL` applies again. `enabled` /
  `allow_private_fetch` are booleans; `max_steps` 1–32, `max_seconds` 5–600,
  `max_result_chars` 500–100000 (out of range → `422`). `permissions` **is
  replaced wholesale** when sent: `{tool_name: "always" | "ask" | "never"}`,
  and a `null` value for one name removes that explicit override (back to
  the risk-based default).
- **The `memory` block** (see *Memory*): `enabled` and `auto_remember` are
  booleans; `prompt_budget_tokens` an integer 100–4000 (the cap for the
  "What you remember" prompt block). Missing keys stay at their saved (or
  built-in) values — same blank-field semantics as the other blocks.
- **The `mcp` block** (see *MCP servers*): `servers` **is replaced wholesale**
  when sent (the web page edits one row at a time by PUTting the whole list).
  Per-server `env` / `headers` **values are write-only** like `llm.api_key`:
  `GET` shows only key names and set-flags (`{"TOKEN": true}`); a PUT with a
  blank value for a key that already exists keeps the stored value, a typed
  value replaces it, and omitting the key removes it. Bad server definitions
  (bad name/transport/command/url, duplicate names, `timeout` < 1) → `422`.
- Numbers validate as before: `422` on non-numeric or negative
  temperature/max_tokens/max_history, or non-object `extra_body`.
- **The `timers` block** (docs/TIMERS.md reviewer additions):
  `allow_chaining` (bool) decides whether a timer-**fired** turn may set
  another timer at all; non-boolean → `422`. Blank = default = `false`.
  Default-overridable with `RIKO_TIMER_ALLOW_CHAINING=1`.

`POST /api/settings/reset` with `{"llm": true, "tts": true, "tools": true,
"memory": true, "mcp": true, "timers": true, "timezone": true, "switch_phrases": true}` (each side optional,
`422` if none)
drops the saved overrides so the defaults apply again. Returns the same body
as `GET`.

`POST /api/settings/test` costs one `GET /v1/models` against the currently
merged settings plus one breeze `GET /health`, and returns
`{"ok": true, "model": "<resolved id>", "tts": {"ok": true}}` or
`{"ok": false, "error": "<reason>", "tts": {"ok": false, "error": "<reason>"}}`
(e.g. `"RuntimeError: LLM not configured"`, `"TTS not configured"`, `"TTS
disabled in settings"`; auth failures surface here). Never reveals the key.

Changing any LLM setting re-resolves `model: "auto"` on the next chat; the
state `version` bumps on every `PUT` and on a `reset` that actually removed
something.

## Memory (long-term facts, docs/WALK_MEMORY.md M)

Beyond the transcript, the hub keeps a small table of **facts** — lasting
things she may need weeks from now ("the user's cat is called Miso"). Three
models-side tools plus owner-side routes and a page tab revolve around it.

**Scopes.** `user` = about the user, shared by every persona.
`persona:<id>` = a private note only that persona ever sees (and it dies with
the persona — `DELETE /api/personas/{id}` removes its notes and publishes a
`memory_changed` for each). Facts carry `id, text, scope, pinned,
source_message_id, created_at, updated_at, last_used_at, use_count`; text is
clamped to one line, ≤500 chars.

**The tools** (`GET /api/tools` lists them; default permission `always`,
editable like any other tool — `never` hides them from the model entirely):

- `remember(text, scope? = "user", pinned? = false)` — stores one fact.
  Near-identical text in the same scope **updates** the existing fact instead
  of duplicating it (response says `"updated": true`). At most 5 facts per
  turn; the turn's saves come back on ChatResult / the assistant message as
  `memory_writes` ("remembered" chip).
- `recall(query, limit? = 5)` — FTS5 keyword search over her scopes
  (`user` + her own notes), ranked; bumps `use_count`. Returns
  `{count, facts: [{id, text, scope, pinned}]}`.
- `forget_fact(id? , text?)` — deletes by id, or by text when it clearly
  matches exactly one fact (exact normalized match wins; a lone search hit
  is also deleted). Ambiguous text returns an error + `candidates` so the
  model can retry with the right id.

**The prompt block.** When memory is enabled, every chat turn gets a
`What you remember` block appended to the system prompt, budget-capped to
`memory.prompt_budget_tokens` (rough char/4 estimate). Ordering: all pinned
facts, then FTS5 matches for the user's current message, then the most
recently used facts; whatever fits keeps its rank, the rest is cut, and the
facts that made it in get their usage bumped. No facts → no block. With
`auto_remember` on, the block also instructs her to call `remember` on her
own when the user shares something lasting; with it off she only saves when
explicitly asked.

Storage note: facts live in the hub SQLite DB with an FTS5 index; on a SQLite
built without FTS5 the store falls back to LIKE search (and backfills the
index if FTS5 appears later).

### GET /api/memory

`?scope=user|persona:<id>` (blank = every scope), `?q=` for FTS-ranked
search, `limit` (1–500, default 200) + `offset` paging. Returns
`{"facts": […], "total": n}` (`query` echoed when `q` is set). `422` on an
unknown scope.

### POST /api/memory → `201`

Add a fact by hand: `{"text": "…", "scope"? , "pinned"?}`. Same dedup as the
`remember` tool — a near-identical fact comes back with `"updated": true`
and the existing id. `422` on empty text or unknown scope.

### PUT /api/memory/{fact_id}

`{"text"? , "pinned"?}` — inline edit / pin toggle from the page. `404`
unknown id, `422` empty text, non-bool `pinned`, or nothing to change.

### DELETE /api/memory/{fact_id} → `204`

Forget one fact (`404` unknown id).

### DELETE /api/memory → `{deleted: n}`

Forget everything. `confirm=yes` is **required** (the page makes the user
type the word first); `scope` picks `all` (default), `user`, or one
persona's notes. Missing confirm → `400`, unknown scope → `404`.

### GET /api/memory/export · POST /api/memory/import

Export: `{"version": 1, "exported_at": …, "facts": [{text, scope, pinned}…]}`.
Import takes that shape (`{"facts": […], "replace"?}`): new ids are
assigned, persona scopes must exist, one bad entry rejects the whole import,
`replace: true` wipes first → `{"imported": n, "replaced": m}`.

```bash
curl -s -H "$AUTH" $HUB/api/memory | jq '.facts[] | [.id, .scope, .text]'
curl -s -X DELETE -H "$AUTH" "$HUB/api/memory?scope=user&confirm=yes"
```

## Timers (one-shot scheduled turns, docs/TIMERS.md)

She schedules them by voice — the model calls the hub tools `timer(prompt,
time, label?, cancel_on?)`, `list_timers()` and `cancel_timer(id|label)` —
and when a timer fires its `prompt` enters the same persona chat as a
user-side message and she answers it, through the normal chat path (emotion,
actions, speech, audio, memory and tools all apply). One-shot only.

- **Times:** relative (`10s`, `+10s`, `5m`, `1h`, `2d`, `in 10 seconds`,
  `5 minutes`) or RFC 3339 **with an explicit timezone** (a bare local time
  is rejected, not guessed). The due time must be ≥ 1 s in the future and at
  most 30 days ahead; the hub holds at most 50 pending timers.
- **Never interrupts:** if a turn is generating for that persona when the
  timer comes due it waits and fires once the chat is free.
- **Survives restarts:** timers live in SQLite and resume on boot; one that
  came due while the hub was down fires shortly after start.
- **Captured at set time:** the fire turn runs with the LLM settings and the
  tool set the setting turn had; the persona's system prompt is rebuilt live.
- **Timezone:** timer times speak the hub's configured zone — the
  `timezone` setting (Settings page; blank = server zone, the hub
  container's `TZ`, `Europe/London` in compose).
- **Asked-for fallback** (docs/TIMERS.md reviewer additions): when the
  owner's message explicitly asks for a timer and the model answers without
  calling the tool, the hub creates the requested timer itself —
  `asked: true` on the timer and in the ChatResult's `timers_set`.
- **Chain guard:** a timer-fired turn does not get the `timer` tool unless
  the `timers.allow_chaining` setting is on, and a chained timer inherits
  `chain_depth` +1 with a hard cap of 3 (`chain limit` tool error past it).

Cancel events (`cancel_on`): `chat.user_message` — the owner sends any
message to that persona first; `chat.read` — the owner opens that persona's
chat (web page, or the desktop window focused → `POST /api/chat/read`).

### GET /api/timers

```bash
curl -s -H "$AUTH" "$HUB/api/timers"              # all recent timers
curl -s -H "$AUTH" "$HUB/api/timers?status=pending"
```

```json
{"timers": [{
  "id": 3, "persona_id": "riko", "label": "tea",
  "prompt": "tell the owner the tea is ready",
  "due_at": "2026-11-05T12:30:00Z",
  "due_at_local": "2026-11-05T12:30:00+00:00", "timezone": "Europe/London",
  "cancel_on": ["chat.user_message"],
  "status": "pending",                             // pending|firing|fired|cancelled|failed
  "asked": false, "chain_depth": 0,
  "created_at": "2026-11-05T12:25:00Z",
  "fired_at": null, "result_message_id": null, "error": null, "retry_count": 0
}]}
```

Newest first, capped at the latest 200. `due_at` is always UTC;
`due_at_local` renders it in the hub's effective timezone for display.
`GET` never returns the captured settings (they contain the write-only API
key). A fired turn's two messages carry `timer_id` — render ⏰ for them.

### DELETE /api/timers/{id} → `{cancelled, label}`

Cancels a `pending`/`firing` timer (`409` when already fired/cancelled,
`404` when unknown). Publishes `timer_cancelled` with `reason: "request"`.
`cancel_timer` from voice publishes the same event with `reason: "tool"`.

### POST /api/chat/read → `{cancelled: [ids]}`

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"persona_id":"riko"}' $HUB/api/chat/read
```

"The owner just read this persona's chat" — cancels that persona's open
timers whose `cancel_on` names `chat.read`. `persona_id` optional (defaults
to the active persona). The web page calls it whenever a persona's chat is
rendered; the desktop calls it on window focus (throttled). Any ordinary
`POST /api/chat` user message cancels `chat.user_message` timers the same
way; the fired timer's own turn never self-cancels.

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
messages / unknown ids. It speaks the message's `speech` text, never its raw
`content` (see *POST /api/chat → ChatResult*): no spoken URLs, markdown or
emoji. Cache key server-side is `(message_id, voice_version, emotion,
speech-rules version)`: editing the persona voice — or the hub upgrading its
speech rules — re-synthesises on the next fetch; old replies keep their old
audio until asked again. The stored emotion adds a matching delivery hint to
the synthesis (see *Emotion tags*) — the tag itself is never spoken.

### GET /api/personas/{id}/greeting/audio

Same, for the greeting (`?v=` informational). This is what a switch's
`audio_url` points at.

### POST /api/tts

One-shot WAV for a line that is not in any transcript (client-side
narration): body `{"text": "…", "persona": optional id, "emotion": optional
one of the emotion ids}`, ≤ **5000 characters** of raw text (`422`), never
cached, `Content-Type: audio/wav`. The text goes through the same
spoken-text cleaner as chat audio (including the 2500-character spoken cap),
so posting a raw reply (markdown and all) still speaks clean words; nothing
speakable after cleaning → `422`. The `emotion`
only steers delivery (same hint as below); unknown emotion → `422`.

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

`{"text": optional, "voice": optional}` — one uncached WAV in this persona's
voice (default text: the greeting). Works even when `mode` is `off`, so you
can audition before enabling.

The optional `voice` override — `{mode, description, style, ref_text, seed,
cfg_scale}`, any subset — is applied on top of the stored voice for **this
one synthesis only**: validated exactly like `PUT /voice` (bad values →
`422`), then thrown away. Nothing is stored, `voice_version`/`state.version`
do not move, and the WAV is not cached. That is how the web page's Preview
plays what you are currently editing, before you press Save. A `clone` mode
(effective after the override) with no stored reference clip → `422`.

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Try me first.","voice":{"mode":"design","description":"A gruff captain.","seed":4242}}' \
  $HUB/api/personas/riko/voice/preview -o try.wav
```

Other errors are like `/api/tts` (`503` TTS off/unreachable, `502` breeze
failed).

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
| `422` | validation (empty name, bad import rows, non-object extra_body, empty `text`, over-long `/api/tts` text, over-long persona `appearance`, bad voice/tts field values, bad client registration incl. malformed `body`) | `{"detail": "…"}` (FastAPI may return its own validation shape) |
| `502` | LLM call failed — **nothing was stored**, safe to retry. Also: breeze synthesis/registration failed | `{"detail": "<ExceptionType>: <reason>"}` |
| `503` | no `llm.base_url` configured; or TTS off/unreachable on an audio route | `{"detail": "LLM not configured"}` / `{"detail": "TTS …"}` |

## Emotion tags

When a persona has `emotes: true` the hub adds one line to the system prompt:
the reply must open with exactly one tag from a **fixed set** —
`neutral, happy, teasing, annoyed, sad, surprised, thinking, embarrassed` —
e.g. `[teasing] Sure thing, senpai.` The hub then:

* **strips the tag** — `reply` / stored `content` / spoken audio never
  contain it (parentheses `(teasing)` are tolerated too);
* reports the parsed id as `emotion` in the ChatResult and on the stored
  Message (missing or unknown tag → `neutral`; `emotes: false` → always
  `neutral` and no prompt line);
* gives Breeze a matching *delivery hint* in `instructions` (e.g. "with a
  playful, teasing lilt") — the emotion changes the voice, not the words;
  the audio cache key includes the emotion.

**Client job:** map `emotion` → your VRM expression blendshapes. It is plain
metadata — you never parse the text, and old clients that ignore it keep
working. The set matches StoryLine's emotion vocabulary, so a client can
share its expression table.

## Body actions (embodiment, docs/BODY.md)

When an **embodied client** (the desktop avatar) is online it registers the
gestures it can actually play (see *Client tool channel*, step 1: the
optional `body` object). The hub keeps each online client's body; the
**body set** is the union of the online clients' `actions` and is **empty
when no embodied client is online** — a client going offline removes its
actions from the set immediately. `GET /api/tools` reports the current set as
`body_actions`.

**Prompt.** When the body set is non-empty *and* the persona has
`embodied: true`, the system prompt gains a short block naming the actual
actions ("You have a 3D body… Available actions: wave, think, nod, …"), so
the model pepper its reply with action tags. When the current user message
explicitly asks her body for one of those actions, the block also gains a
one-line nudge (`The user just asked you to wave: include [wave] in your
reply.`). The persona's `appearance` line (`Your appearance: …`) is appended
whenever `appearance` is non-empty — body online or not.

**Parsing (what the hub accepts).** Applied to the final reply *after*
emotion-tag stripping; names are matched case-insensitively, hyphens and
spaces fold to `_`:

| model writes | in body set | not in body set |
|---|---|---|
| `[wave]` anywhere in the reply | action `wave` | known gesture name → **stripped + logged as ignored**; otherwise left in the text untouched (`[sad]`, `[note]`, `[Stardust](url)` survive) |
| `[action:spin]` / `[action-spin]` | action `spin` | **always stripped**, logged as ignored (an explicit request the body can't do) |
| `*waves*` `*nods*` `*thinks*` `*shakes head*` `*stretches*` `*looks around*` `*laughs*` `*pouts*` `*touches hair*` | mapped via the verb table (`waves → wave`, …) and stripped | stripped + logged as ignored |
| `*smiles*` (not in the verb table) | left alone (prose emphasis survives) | |

Clean text: no tags/asterisk actions remain in stored or spoken text
(spaces/punctuation collapsed; Breeze audio is built from the clean text).
**Limits:** at most 3 actions per reply (extras dropped), adjacent
duplicates merged.

**Asked-for fallback (docs/BODY.md §3).** Whether the model emits tags is
stochastic (worse at high temperature), so the hub also inspects the **user's
message**: if it explicitly asks her body for an action of the current body
set — conservative imperative/request forms like "wave at me!", "can you
nod?", "give me a wave", "show me your thinking pose", "touch your hair"
(mentions do **not** count: "I waved at my friend", "what do you think?",
"wave function", "don't wave") — then:
* if the reply's `actions` already contain it, that action object gains
  **`"asked": true`**;
* if they don't, `{"name": ..., "at": 0.0, "asked": true}` is **added at the
  start of the reply** (within the 3-action cap).
The fallback never runs for a non-embodied persona, with no embodied client
online, or for a gesture outside the body set. `asked` is an **additive
field** — present only when `true`; action objects otherwise keep their
shape, so old clients ignore it. Embodied clients should treat
`asked: true` as the docs/BODY.md §5 "asked for" class: always play it,
bypassing cooldowns (this replaces client-side verb matching of the user's
text).

**Timing.** Each action carries `at`, the 0..1 fraction of the *cleaned*
reply's length where the tag appeared (`0` = before the first word). Clients
play it at `at * speech duration` — the hub does not know your audio length.

**Where actions show up:** `ChatResult.actions` (always present, `[]` when
none), `Message.actions` on stored assistant messages, `turn_finished`
events, and switch greetings (`[{"name": "wave", "at": 0.0}]` when the
persona is embodied and the body set has `wave`). On tool-loop turns actions
come from the **final** reply only. Like emotions, this is plain metadata:
`reply`/`content` never contain the raw tags and old clients that ignore
`actions` keep working.

**Client job (embodied clients):** play each action against the speech
(docs/BODY.md §5); ignore unknown names with a console note.

## Tools

The hub runs an OpenAI function-calling loop inside `POST /api/chat`: the
model may call tools, the hub executes them and feeds the results back, and
only the final text answer is stored/spoken. Stored history keeps only your
message + the final reply + a compact `tools_used` list; the full trace goes
to the tool log. Tool/web results are wrapped as **untrusted data** (the
model is told they are not instructions) and capped at `max_result_chars`.

Built-in hub tools:

| tool | what it does |
|---|---|
| `web_search` | SearXNG JSON search → `{results: [{title, url, content}]}`. Needs a `searxng_url` setting/env (`RIKO_SEARXNG_URL`); **not offered** when unset |
| `fetch_page` | GET a URL, extract readable text (title + paragraphs/heads/lists, ≤ 20 000 chars, ≤ 10 MB, ≤ 20 s). Refuses loopback/private IPs unless `allow_private_fetch` |
| `get_time` | hub clock as ISO-8601 with UTC offset |

Permissions per tool name, effective policy (`GET /api/tools` shows all of
it): an explicit `settings.tools.permissions[name]` wins (`always` / `ask` /
`never`); otherwise hub tools are `always`, client tools default from their
declared `risk` (`read` → `always`, `act` / `danger` → `ask`). `never` tools
are never offered; `ask` waits for a human yes/no over the client-tool
channel (see below) — up to 120 s, then treated as *not approved*.

Function-calling health: `GET /api/tools/status` →
`{"function_calling": "ok" | "rejected" | "unknown"}`. If the model server
400s the `tools` parameter itself the hub falls back to plain one-shot
answers and marks `rejected` until a later turn works again.

```bash
curl -s -H "$AUTH" $HUB/api/tools
# {"tools":[{"name":"web_search","source":"hub","risk":"read","permission":"always","available":true,"description":"…"}, …],
#  "settings":{…merged tools block…},"function_calling":"ok",
#  "body_actions":["wave","think","nod",…]}   // online body set (see *Body actions*)

curl -s -H "$AUTH" "$HUB/api/tools/log?limit=50"
# {"entries":[{"id":9,"created_at":"…","persona":"riko","tool":"web_search",
#   "source":"hub","arguments":"{\"query\": \"tokyo weather\"}","ok":true,
#   "error":"","ms":812,"approval":""}, …]}
# entries also carry source:"client" rows; approval is "", "approved",
# "denied" or "not approved" for gated calls
```

## MCP servers (the hub as an MCP client)

The hub can connect to external **MCP servers** and offer their tools to the
model next to the built-in ones. Configure them in **Settings > MCP** on the
web page (rows save immediately) or over the API. The MCP SDK is imported
lazily: without the `mcp` package installed the hub still boots and runs, and
every configured server reports `state: "unavailable"` with error
`"mcp package not installed"`.

Server shape (in `settings.mcp.servers`; the whole list is replaced on every
`PUT`, values of `env`/`headers` are **write-only** — with one exception, the
`${NAME}` placeholders below):

```json
{"name": "time",                     // [a-z0-9_-]{1,32}, unique
 "transport": "stdio",               // or "http" (streamable-HTTP)
 "command": "uvx", "args": ["mcp-server-time"],   // stdio only
 "env": {"TZ": "Asia/Tokyo"},        // stdio only; extra env for the child
 "url": "http://127.0.0.1:8931/mcp", // http only (http/https)
 "headers": {"X-Key": "***"},   // http only; extra request headers
 "enabled": true,
 "default_permission": "always",     // optional soft default for this server's
                                     // tools: always|ask|never (empty = per
                                     // tool risk); beaten by tool_permissions
                                     // and the global tools.permissions map
 "tool_permissions": {"delete_file": "never"},     // per *server-side* tool name,
                                                   // always|ask|never; beats the
                                                   // global tools.permissions map
 "timeout": 60}                      // per-call seconds, ≥ 1
```

**`${NAME}` placeholders in `env`/`headers` values** are secrets-by-
reference: at connect time the hub expands `${NAME}` from its own process
environment (names must match `^[A-Z][A-Z0-9_]{0,63}$`; anything else stays
literal). A missing variable puts the server in
`state: "error"` with `error: "missing env NAME"` — names only, never a
value — and the rest of the hub keeps running. The expanded value is never
stored, logged or returned: `GET /api/settings` keeps exactly what you saved.
A value that is pure literal text + placeholders (e.g.
`Bearer ${RIKO_HA_TOKEN}`) is **not masked** on reads, because the raw
template holds no secret; a value with any real characters in it masks as
the usual write-only bool. This is how the Home Assistant token stays in
`~/AI/Docker/riko-hub/.env` and out of the database and the web page.

How the tools join the loop:

- Model-facing name is **`mcp_<server>__<tool>`** (sanitized to
  `[A-Za-z0-9_-]{1,64}`), e.g. `mcp_time__get_current_time`. Home
  Assistant's own domain prefixes survive: `intent__HassTurnOn` becomes
  `mcp_homeassistant__intent__HassTurnOn`.
- Risk comes from the server's tool annotations: `readOnlyHint` → `read`
  (offered `always`), `destructiveHint` → `danger`, otherwise `act`. The
  permission ladder per tool: `tool_permissions` override → saved global
  `tools.permissions` → the server's `default_permission` → risk default
  (`always` for `read`, else `ask`). `never` hides the tool entirely. `ask`
  approvals ride the same `__approval__` client channel as client tools.
- **Prompts travel too**: on (re)connect the hub fetches each ready server's
  MCP Prompts that need no arguments (`list_prompts`/`get_prompt`) and
  appends them to the system prompt as `Instructions from <server>:` blocks
  (≤ 1500 chars per server, ≤ 3000 total). Servers without prompt support,
  or with only templated prompts, are silently skipped. Resources are not
  auto-read — live-state tools cover that.
- Calls appear in `/api/tools`, `/api/tools/log` (`source: "mcp"`) and the
  events stream (`tool_started` with label `using <server>: <tool>`, or a
  readable phrase for Home Assistant tools, e.g. `turning on Kitchen light`
  from `intent__HassTurnOn` args `name`/`area`, `checking the house` for
  `GetLiveContext`); results are capped at `max_result_chars` and wrapped as
  untrusted like everything else. `tools.enabled: false` turns MCP tools off
  too.
- Lifecycle: connecting happens on a dedicated MCP event loop; a dead or
  silent server is retried with exponential backoff, the tool list refreshes
  on (re)connect and on `notifications/tools/list_changed` when supported,
  and a bad per-call outcome never kills the turn — the model just gets an
  error payload.

```bash
curl -s -X PUT -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "mcp": {"servers": [
    {"name": "time", "transport": "stdio", "enabled": true,
     "command": "uvx", "args": ["mcp-server-time"]}]}}' $HUB/api/settings

curl -s -H "$AUTH" $HUB/api/mcp/status
# [{"name":"time","transport":"stdio","state":"ready","error":"",
#   "tools":1,"last_ok":"2026-11-03T18:30:00Z"}]
# state: starting | ready | error | disabled | unavailable; error carries the
# last failure; last_ok is the time the server last listed its tools

curl -s -X POST -H "$AUTH" $HUB/api/mcp/time/restart   # force reconnect
# {"ok":true}   ·   404 unknown name   ·   409 disabled
```

A failed `PUT`/startup sync publishes an `mcp_error` event and (for a PUT)
answers `422` — a broken server never bricks chat. In Docker, stdio servers
run **inside the hub container**: `uvx` and `npx` are installed there and
their caches are writable under `/data` (see `hub/README.md`); an http server
pointing at the host needs a host-reachable URL.

### Built-in Home Assistant server (`RIKO_HA_URL` + `RIKO_HA_TOKEN`)

When both `RIKO_HA_URL` and `RIKO_HA_TOKEN` are set in the hub's environment
(the compose file passes them through from `~/AI/Docker/riko-hub/.env`) and
nothing named `homeassistant` is saved in settings, the hub offers a
built-in MCP server for Home Assistant's official **Model Context Protocol
Server** integration:

```json
{"name": "homeassistant", "transport": "http", "enabled": true,
 "url": "<RIKO_HA_URL>/api/mcp/assist",
 "headers": {"Authorization": "Bearer ${RIKO_HA_TOKEN}"},
 "default_permission": "always"}
```

- `/api/mcp/assist` is the integration's *Device control* endpoint: the same
  tools HA Assist has (`intent__HassTurnOn`, `light__HassLightSet`,
  `homeassistant__GetLiveContext`, …), restricted to the entities you
  exposed under **Settings > Voice assistants > Expose**. The integration's
  main endpoint `/api/mcp` exposes more; add it as a normal MCP server by
  hand if you want it.
- `default_permission: "always"` matches the owner's call: Riko is the
  voice assistant and HA is the hands — whatever you exposed, she may use
  without asking. Per-tool `tool_permissions` still wins, so you can pin
  anything to `ask` or `never`.
- Env-default semantics like every other settings block: the row shows
  `mcp_source: {"servers": "env"}`, editing or disabling it on **Settings >
  MCP** saves your version (source flips to `saved`), and
  `POST /api/settings/reset {"mcp": true}` returns to the env default.
- The Assist integration also publishes an MCP **Prompt** with Assist's
  instructions; the hub picks it up automatically (see *Prompts travel too*
  above), so Riko gets the same guidance Assist uses, plus a standing hint
  to speak device names naturally, never entity ids.
- The token never enters the database or the web page: the saved header is
  the literal text `Bearer ${RIKO_HA_TOKEN}`; only the hub process, which
  reads `RIKO_HA_TOKEN` from its own environment at connect time, ever sees
  the value.

## Events long-poll

Live progress without websockets (any HTTP client can follow — this is the
same long-poll trick as the client-tool channel):

```bash
curl -s -H "$AUTH" "$HUB/api/events?after=0&wait=25"
# {"events":[
#   {"seq":12,"time":"2026-11-03T18:30:01Z","type":"turn_started","persona":"riko","source":"android"},
#   {"seq":13,"time":"…","type":"tool_started","call_id":"…","tool":"web_search","label":"searching the web for “tokyo weather”"},
#   {"seq":14,"time":"…","type":"tool_finished","call_id":"…","tool":"web_search","ok":true,"ms":812},
#   {"seq":15,"time":"…","type":"approval_needed","request_id":"…","client_id":"my-desktop","tool":"desktop__do_thing","summary":"do_thing {}"},
#   {"seq":16,"time":"…","type":"turn_finished","persona":"riko","message_id":42,"emotion":"happy","actions":[{"name":"wave","at":0.0}]},
#   {"seq":17,"time":"…","type":"memory_changed","id":7,"op":"added"},
#   {"seq":18,"time":"…","type":"timer_set","id":3,"label":"tea","persona_id":"riko","due_at":"2026-11-05T12:30:00Z","due_at_local":"2026-11-05T12:30:00+00:00","cancel_on":[],"asked":false},
#   {"seq":19,"time":"…","type":"timer_fired","id":3,"label":"tea","persona_id":"riko","message_id":42},
#   {"seq":20,"time":"…","type":"turn_finished","persona":"riko","origin":"timer","persona_id":"riko","timer":{"id":3,"label":"tea"},"message_id":42,"reply":"Your tea is ready!","speech":"Your tea is ready!","emotion":"happy","actions":[],"audio_url":"/api/messages/42/audio"},
#   {"seq":21,"time":"…","type":"state_changed","version":21}
# ],"last":21}
```

Loop: keep `last`, pass it back as `after`, `wait` ≤ 30 s (default 25). The
call returns immediately when anything new happens, otherwise after `wait`
with an empty list — treat an empty return as "nothing happened, poll
again". `seq` is monotonic from hub start (last 500 events are buffered; if
your `after` is older than that you get everything still buffered). Show
`tool_started.label` as activity text ("🔧 searching the web…") while your
chat POST is still in flight; `turn_finished` tells you the turn is done.
`state_changed` mirrors the `/api/state` version bumps, so you can replace
part of your state polling with this one connection. `memory_changed`
(`{id, op}` with `op` = `added` / `updated` / `deleted`) fires whenever a
fact is saved, edited or forgotten — by the memory tools, the routes, or a
persona delete that took its private notes with it; refresh your memory view
on it. Timers (docs/TIMERS.md) publish `timer_set` /
`timer_cancelled` / `timer_fired` (`{id, label, persona_id, …}`; cancelled
also carries `reason`: `user_message`, `chat_read`, `tool`, `request`,
`persona_deleted`), plus `timer_retry` / `timer_failed` when
a fire turn hits the LLM once more before giving up. When the fire turn
finishes, `turn_finished` carries the FULL speak payload so you can play
the proactive reply without another round-trip: `origin: "timer"`,
`persona_id`, `timer: {id, label}`, `reply`, `speech`, `emotion`, `actions`,
`audio_url` (and the usual `persona`, `message_id`, `emotion`). Desktop
clients: play origin-timer turns like normal replies; queue them if busy.

## Client tool channel (desktop / Android tools the model can call)

A running app can expose its own tools; the hub pushes the work to the app
over HTTP long-poll and feeds the result back to the model. Model-facing
name is **`<kind>__<name>`** (double underscore), e.g. `desktop__open_url`.

Protocol (client side, all with `$AUTH`):

1. **Register** at startup — `POST /api/clients/{id}/register` with
   `{name, kind, platform?, version?, tools: [{name, description,
   parameters?, risk?}], body?}`. `id` matches `[A-Za-z0-9_-]{1,64}` (e.g.
   `my-desktop`), `kind` is a lowercase word (`desktop`, later `android`),
   tool `name` matches `[A-Za-z0-9_-]{1,64}`, `risk` is `read` (default),
   `act` or `danger`. Re-register with the same `id` any time your tool
   list changes — it replaces the whole registration.
   `body` is optional and declares what a 3D body can do right now (see
   *Body actions*): `{"actions": ["wave", "think", …], "expressions":
   ["happy", …]}` — lists of gesture/expression names matching
   `[a-zA-Z0-9_-]{1,64}` (a client without a body sends no `body`; `422` on
   a malformed one). The hub keeps it per online client and drops it when
   the client goes offline; re-register whenever the gesture library
   changes (a `.vrma` drop-in).
2. **Long-poll for work** — `GET /api/clients/{id}/requests?wait=25`
   (wait ≤ 60 s) returns immediately when there is queued work, else after
   `wait`; body is `{"requests": [...], "approval_timeout_s": 120}` with an
   empty list meaning "nothing yet". Each request: `{"request_id", "tool",
   "arguments", "needs_approval", "summary", "deadline"}`. `tool` here is
   the **bare name** (`open_url`, not `desktop__open_url`). A request with
   `tool: "__approval__"` is approval-only (a hub tool gated to `ask`): show
   `summary`, answer, run nothing.
3. **Execute (or refuse)** and post the result once per request:
   `POST /api/clients/{id}/results` with `{"request_id", "ok": true,
   "approved": true|false, "result": <json|text>, "error": ""}`.
   `approved: false` means the human said no — the model is told it was not
   approved. If you never answer an approval-gated call, it times out
   (120 s) as *not approved*.
4. **Stay online** — re-register or `POST /api/clients/{id}/heartbeat`
   every ≤ 30 s. No contact for 75 s → the client and its tools disappear
   from `/api/tools` and the model stops seeing them (in-flight calls fail
   fast). `DELETE /api/clients/{id}` on clean exit. `GET /api/clients`
   shows who is online.

Worked example — a desktop app exposing `open_url` (risk `read`, so it runs
without asking):

```bash
H=http://192.0.2.51:8002; AUTH='Authorization: Bearer test'

# 1. register
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "name": "Lain desktop", "kind": "desktop", "platform": "arch",
  "tools": [{"name": "open_url", "risk": "read",
             "description": "Open a URL in the user's default browser",
             "parameters": {"type": "object",
                            "properties": {"url": {"type": "string"}},
                            "required": ["url"]}}],
  "body": {"actions": ["wave", "think", "nod", "shake_head", "look_around",
                       "stretch", "fidget", "hair_touch", "laugh", "pout", "listen"],
           "expressions": ["happy", "angry", "sad", "relaxed", "surprised"]}
}' $H/api/clients/my-desktop/register
# {"ok":true,"model_tools":["desktop__open_url"],"heartbeat_seconds":30,...}

# 2. in a background loop: wait for work
curl -s -H "$AUTH" "$H/api/clients/my-desktop/requests?wait=25"
# {"requests":[{"request_id":"3f9c…","tool":"open_url",
#   "arguments":{"url":"https://docs.godotengine.org"},
#   "needs_approval":false,"summary":"open_url {\"url\": \"https://…\"}",
#   "deadline":"2026-11-03T18:31:02Z"}],"approval_timeout_s":120}

# 3. open the browser, then report
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"request_id":"3f9c…","ok":true,"approved":true,"result":"opened","error":""}' \
  $H/api/clients/my-desktop/results
```

Meanwhile the user asked Riko "open the godot docs for me"; `/api/chat`
returns once the model had the tool result. An approval-gated flow looks
identical except `needs_approval: true` — you decide, then post the result
with `approved: true/false`. Clients may run **multiple concurrent**
requests (different `request_id`s) — the hub issues independent calls in
parallel.

### The browser link (desktop ↔ Riko Helper, loopback only)

The desktop's `BrowserBridge` (`riko/client_tools.py`) listens on
`desktop.client.browser_port` (default 8766, 127.0.0.1 only) and answers
three plain GET routes next to its token-authenticated WebSocket:

- `GET /hello` → `{"ok": true, "name": "riko-desktop", "version": "…"}` —
  **no auth**, `Access-Control-Allow-Origin: *`. This is the extension's
  reachability probe: it opens its link tab only when this answers with our
  identity within 2 s; anything else (refused, 404, foreign JSON) means
  offline, and then no tab opens at all. Retries ride a 1-minute alarm with
  exponential backoff capped at 4 minutes; a manual tab close gets a 60 s
  reopen cooldown, and the options page can disable auto-reopen entirely
  (the popup's Connect then opens it on demand).
- `GET /riko-pair` → the pairing token as `text/plain` (also CORS `*`).
- `GET /link` → the quiet host page (`text/html`, CORS `*`) whose tab
  carries the actual `ws://127.0.0.1:<port>/` connection: an http origin is
  the only place Firefox still allows plaintext `ws://` (the extension's own
  secure context force-upgrades to `wss://`). It is a friendly status page
  ("Riko is online/offline", auto-reconnect notice) — when the desktop is
  down the tab simply isn't there, so no raw browser error page shows.

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
   again" and `502` as "retry later, history is intact". Optionally replace
   part of this polling with one long-running `GET /api/events` connection
   (*Events*), render `emotion` from chat results / history onto your VRM
   face (*Emotion tags*), and watch tool activity live while a chat is in
   flight (*Events*).
