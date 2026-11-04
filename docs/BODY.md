# Embodiment: she knows she has a body (contract)

Status: approved by the owner (2026-11-04). Children follow this literally and report problems instead of
editing it. Builds on docs/API.md (hub API), docs/ROUNDS.md (emotion tags, client-tool channel, events) and
the avatar director in client/js/director.js.

## Goal

When an embodied client (the desktop avatar) is online, she knows she has a 3D body on the owner's desktop
and acts with it. She waves when asked ("wave at me"), gestures naturally while talking, and can describe
what she looks like. Clients without a body (a future phone app, the web chat box) aren't affected.

## 1. Body capabilities (desktop → hub)

`POST /api/clients/register` gains an optional `body` object:

```json
"body": {
  "actions": ["wave", "think", "nod", "shake_head", "look_around", "stretch", "fidget",
              "hair_touch", "laugh", "pout", "listen"],
  "expressions": ["happy", "angry", "sad", "relaxed", "surprised"]
}
```

- `actions` is the list of gesture names the client can actually play right now. The desktop builds it
  from the director/gesture library (built-in poses plus any `.vrma` drop-ins), excluding internal names
  (`talk_*`).
- The hub keeps the body of each online client. The **body set** is the union of the online clients'
  `actions`, and is empty when no embodied client is online.

## 2. Prompt (hub)

When the body set is non-empty, the persona prompt gains a short block. Keep it within about 120 tokens and
include the actual names:

> You have a 3D body: you stand on the user's desktop and they can see you. You can move by putting action
> tags anywhere in your reply, e.g. `[wave]`, `[think]`, `[nod]`. Available actions: wave, think, nod, ….
> When the user asks you to do one, do it (and you may say a few words about it). Otherwise use them
> sparingly and naturally, like a person gesturing while talking; usually zero or one per reply. Never
> describe actions in prose like *waves*; use the tags.

- **Persona `appearance`** (new, text, default empty, max 1000 chars). When set, append "Your appearance: …".
  The appearance line is included even without a body, so she can still describe herself in text-only chats.
- **Persona `embodied`** (new, bool, default true). When false, the body block is never added for that persona.

## 3. Parsing (hub)

- **What counts as a tag.** After the emotion-tag stripping, find `[name]` and `[action:name]` tags whose
  name (case-insensitive) is in the body set. Also accept roleplay asterisks like `*waves*`, `*nods*`,
  `*thinks*`, `*stretches*`: map a small verb table (`waves → wave`, `nods → nod`, `shakes head →
  shake_head`, `thinks → think`, `stretches → stretch`, `looks around → look_around`, `laughs → laugh`,
  `pouts → pout`, `touches hair → hair_touch`) and strip the asterisk span.
- **Unknown tags.** Leave unknown bracket tags untouched, except a bare `[something]` that looks like an action
  request for a name not in the body set: strip it and log it as `ignored_actions`.
- **Clean text.** No tags or asterisk actions in the stored or spoken text. Collapse double spaces and stray
  punctuation.
- **Timing.** Each action gets the character offset where it appeared in the cleaned text:
  `actions: [{"name": "wave", "at": 0.0}]`, where `at` is a 0..1 fraction of the reply's length (0 = before
  the first word). Clients use it to time the gesture against speech.
- **Limits.** At most 3 actions per reply (extras dropped). Duplicates next to each other are merged.
- **Where it shows up.**
  - `ChatResult.actions` (always present, `[]` when none) and `Message.actions` on stored assistant messages.
  - The `turn_finished` event gains `actions`.
  - Switch greetings get `actions: [{"name": "wave", "at": 0}]` when the body set has `wave`.
- **Breeze audio.** Unaffected (it's made from the clean text).

## 4. Web page (hub)

- The persona editor gets an **Appearance** textarea, with a hint: "how she looks: hair, eyes, outfit, …;
  she uses it to describe herself", and an **Embodied** toggle.
- History and Chat show actions as small chips next to replies.
- Settings > Tools shows the online body set.

## 5. Desktop (client)

- **Registering.** Register with the hub including `body` (see 1). Refresh the registration when the
  gesture library changes (a VRMA drop-in, or a persona swap that changes the available set).
- **Playing.** On a chat result (and on `turn_finished` events from other clients' turns), pass `actions` to the
  page in the `say` event (`actions: [{name, at}]`). The director schedules each action at
  `at * speech duration` (the audio clip length, or an estimate from the text when there's no audio):
  - **asked for** = the action appears in a reply to a user message that names it ("wave", "think", "nod",
    "stretch" …). It always plays, like the W shortcut: it bypasses cooldowns and preempts idle/talk/emotion
    one-shots.
  - **natural** = any other action. It plays if free, honours the cooldowns, and is never queued for more
    than a few seconds.
  - Waves keep the happy face.
  - The hub doesn't flag asked-for actions. The director decides from the user's last text, using the same
    verb table, mirrored in `client/js/director.js`.
- **Unknown action names** are ignored, with a console note.

## 6. Ownership (this round, two children in parallel)

| path | owner |
|---|---|
| `hub/**`, `tests/test_hub_*.py`, `docs/API.md` | **H** |
| `client/**`, `riko/**`, `desktop.py`, `main.py`, `bin/**`, `character_config.yaml`, `README.md`, non-hub tests | **C** |
| `docs/BODY.md`, `docs/*.md` other than API.md | reviewer |

C may use the fake hub in tests/conftest.py, and should add `actions` to it. H tests with a fake client that
registers a `body`.
