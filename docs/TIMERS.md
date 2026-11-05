# Timers contract

Status: owner request (2026-11-05), modelled on Open WebUI's timer builtin
(docs.openwebui.com > Chat features > Timers). Starts after the "speak the whole reply" fix lands.
Two children with disjoint files: **H** (hub) and **D** (desktop). Children follow this literally and
report problems instead of editing it.

## Behaviour (same as Open WebUI)

- A timer lets her come back later. The model calls a hub tool `timer` with a `prompt` and a `time`. When
  it fires, the prompt is appended to **the same persona chat** as a user-side message, and she answers it
  as if you had typed it, with the whole conversation as context.
- **Times:**
  - relative: `10s`, `5m`, `1h`, `2d`, `+10s`, `in 10 seconds`, `5 minutes`, `2 hours` (units: seconds,
    minutes, hours, days);
  - or an RFC 3339 timestamp **with an explicit timezone**. One without a timezone is rejected, not
    guessed. The time must be in the future (≥ 1 s).
- **One-shot only.** Recurring schedules are out of scope.
- **Cancelling events** (`cancel_on`, optional list):
  - `chat.user_message`: the owner sends a message in that persona chat, from any client, before the
    timer fires;
  - `chat.read`: the owner opens that persona's chat on the web page (History / Chat tab for that
    persona) or the desktop window becomes visible and focused while that persona is active.
  - "Remind me in an hour unless I get to it" = `cancel_on: ["chat.user_message"]`.
- **Never interrupts:** if a turn is generating for that persona when the timer comes due, it waits and
  fires once the chat is free.
- **Captured when set:** persona id, the LLM settings and the tool set of the setting turn, and the
  memory/embodiment state (rebuilt on fire from the same pieces, like a normal turn). Later edits to the
  persona do not change a pending timer's persona id, but its system prompt is rebuilt at fire time.
- **Survives restarts:** stored in SQLite and resumed on boot. A timer that came due while the hub was
  down fires shortly after start (late rather than never).
- **Scheduler:** polls every 1 s (`RIKO_TIMER_POLL_S`, minimum 1) and claims up to 10 due timers per
  poll.

## Additions beyond Open WebUI (owner-friendly)

- Tools `list_timers()` and `cancel_timer(id)`, so "what timers do I have?" and "cancel the tea timer"
  work by voice.
- The web page gets a small **Timers** list (pending/fired/cancelled, with countdown and a cancel button).
- A fired timer's reply carries `timer: {id, label}` and shows a ⏰ chip in History/Chat.
- Each timer has a short `label` (the model writes it, e.g. "tea"; it defaults to the first words of the
  prompt) for listing and cancelling.
- Caps: 50 pending timers, at most 30 days ahead. These are sanity limits, documented.

## H — hub

- **Store:** a `timers` table:
  - `id`, `persona_id`, `label`, `prompt`, `due_at` (UTC ISO)
  - `cancel_on` (JSON), `captured` (JSON: settings/tool ids)
  - `status`: pending | firing | fired | cancelled | failed
  - `created_at`, `fired_at`, `result_message_id`, `error`
  - Migration via user_version.
- **Tools** (registry, permission `always` by default): `timer(prompt, time, label?, cancel_on?)`,
  `list_timers()`, `cancel_timer(id | label)`. `timer` returns the id plus the due time in the owner's
  local time, so she can say "OK, I'll remind you at 14:35". Timezone: the hub's `TZ` (compose
  `TZ: ${TZ:-Europe/London}`) and a settings field `timezone` (blank = server TZ). The prompt gets the
  current local date/time line (if a time line already exists, reuse it).
- **Scheduler thread:**
  - claim due rows atomically (pending → firing);
  - wait on the persona's chat lock (never interrupt);
  - run a normal chat turn with the timer prompt (stored as a user message marked `timer_id`, shown with
    ⏰);
  - mark fired with `result_message_id`, or failed with the error (retry once after 30 s on an LLM
    error, then failed).
  - The turn goes through the same path as `/api/chat`: emotion, actions, speech, audio_url, memory
    and tools all apply.
- **Events:**
  - `timer_set` / `timer_cancelled` / `timer_fired {id, label, persona_id, message_id}`;
  - plus the normal `turn_finished` with the reply.
  - Event payloads must carry everything a client needs to show and speak the reply: `reply`, `speech`,
    `emotion`, `actions`, `audio_url`, `persona_id`, `message_id`, `origin: "timer"`.
- **Cancel hooks:**
  - any `/api/chat` user message for persona P cancels P's pending timers with `chat.user_message`
    (the timer-fired turn itself must not count);
  - `POST /api/chat/read {persona_id}` (called by the web page when a persona's chat is shown, and by the
    desktop on focus) cancels `chat.read` timers.
- **API:** `GET /api/timers?status=`, `DELETE /api/timers/{id}`, `POST /api/chat/read`, all documented
  in docs/API.md.
- **Web page:** the Timers list and the ⏰ chip, and the page calls `/api/chat/read` when a persona's
  chat is opened or focused.
- **Tests:**
  - parsing (all the relative forms, RFC 3339 with/without timezone, past times, caps), store and
    migration;
  - scheduler with an injected clock (no real sleeps over 1 s): fires, waits for a busy chat, survives a
    restart (rows persist, late fire), batch of 10;
  - cancel_on both events (and the fired turn not self-cancelling), list/cancel tools, events payload,
    fake-LLM end-to-end;
  - page routes test.
- **REAL check:** against the real LLM on a local hub, "remind me in 20 seconds to check the oven": show
  the tool call, the pending row, and the fired reply ~20 s later.

## D — desktop

- Follow the hub events long-poll (riko/hub_client events API; the desktop may already poll for persona
  switches — reuse it). On `turn_finished` with `origin: "timer"` for the active persona: play it exactly
  like a normal reply:
  - speech bubble, her voice from `audio_url` (hub mode), emotion, body actions;
  - if not idle (she's speaking, listening or thinking), queue it after the current turn.
  - Timer turns for a non-active persona: show a small tray notification "⏰ <persona>: <label>" and do
    not switch persona.
- On window show/focus, call `POST /api/chat/read {persona_id: active}`. Throttle to once per 30 s per
  persona.
- **Always-listen:** pause the VAD while a timer reply plays, through the same hold/echo-tail path as
  normal turns.
- **Tests:**
  - fake hub events: timer reply played with audio, queued while busy, other-persona notification;
  - read call on focus, throttled;
  - old hub without timers: nothing breaks.

## Ownership

| path | owner |
|---|---|
| `hub/**`, `tests/test_hub_*.py`, `docs/API.md`, hub compose/.env.example | H |
| `riko/**`, `desktop.py`, `main.py`, `client/**`, `bin/**`, `character_config.yaml`, non-hub tests, README (timers paragraph) | D |
