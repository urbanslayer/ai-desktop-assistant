# What this fork changes vs upstream

Detailed list of everything added or changed on top of the upstream
[Project Riko 50K snapshot](https://github.com/rayenfeng/riko_project)
(merge-base `3ac7a92`). Upstream's core loop — record → transcribe →
reply → speak → lip-synced VRM avatar — is kept; this fork grows it into a
desktop assistant: a central hub on a GPU box, a PySide6 desktop app that
lives on your screen, tools with permissions, touch, self-authored
animations, Spotify control and much more. Each bullet points at the doc
that specifies and verifies it.

## Architecture: the hub

- **Central hub server** (`hub/`): FastAPI + SQLite holding personas, chat
  history, LLM settings, voices, timers and animations; every client
  (desktop app, web page, terminal, future phone app) talks one versioned
  HTTP API with a bearer key, so they share one head.
  [docs/DESIGN.md](DESIGN.md), [docs/API.md](API.md),
  [`hub/README.md`](../hub/README.md)
- **Docker packaging** for the hub (`hub/Dockerfile`, `hub/compose.yaml`,
  `.env.example`): every module ships in the image and every `RIKO_*`
  variable is forwarded, guarded by a drift test.
- **Personas**: any number of personalities, each with prompt, greeting,
  keywords, `.vrm` avatar and voice; switch from the web page, by spoken
  keyword, from the tray, or `riko-ctl persona <name>` — every client
  follows live, no reload.
- **Chat history on the hub**: browse, edit, select, delete entries, save
  a message to memory; per-persona, shared across clients.
  [docs/API.md](API.md)
- **Long-term memory**: a facts store with FTS5 search, `remember` /
  `recall` / `forget_fact` tools, a budgeted "What you remember" prompt
  block, pins, cross-scope dedup, a Memory tab on the web page.
  [docs/API.md](API.md)
- **Versioned SQLite schema** with tested migrations (v1→v10: voices,
  timers, message meta, animations…), settings semantics that cannot be
  blanked by a careless save, and an event stream (long-poll) that clients
  follow. [docs/API.md](API.md)
- **Nightly backups**: hub snapshot via the sqlite3 backup API in
  `scripts/backup_hub.sh` (tar.zst, retention), a pull script + systemd
  timers for a second copy, user-facing timers.
  [docs/BACKUPS.md](BACKUPS.md), `scripts/`
- **LLM robustness**: reasoning models no longer eat the whole token
  budget into a silent reply (cut-off reply guard: retry once with double
  budget, thinking allowed); empty text is never sent to TTS.

## Desktop app

- **PySide6 desktop app** (`desktop.py`): borderless, transparent,
  always-on-top floating VRM window (QtWebEngine), system tray with
  persona / listen mode / name gate / dictation / awareness / touch
  toggles, greeting on start. `desktop/hyprland/` ships window rules and
  suggested global binds; `desktop/riko.desktop` for autostart.
- **Roam strip**: `desktop.roam: strip` turns her window into a
  see-through band along the bottom of the screen; she walks along it,
  turns at edges, stops to gesture; everything but her body is
  click-through (XFIXES input region tracking her projected bbox).
  [docs/WALK_MEMORY.md](WALK_MEMORY.md)
- **Scroll to resize**: plain scroll sizes her window (feet anchored,
  monitor-clamped, per-persona scale remembered, real logical monitor
  coords); Ctrl+scroll stays the camera close-up zoom.
- **App-specific shortcuts, Super-free**: in-window keys fire only when
  she's focused, all configurable under `desktop.shortcuts:`, any binding
  using Super/Meta is rejected at startup. Suggested Hyprland binds moved
  to Ctrl+Alt. `bin/riko-ctl` drives the running app from anywhere.
  (tests/test_shortcuts.py)
- **Offline vendored JS**: the avatar page loads three.js,
  `@pixiv/three-vrm` and `@pixiv/three-vrm-animation` from
  `client/vendor/` (checked in verbatim, SHA-256-pinned, import map) —
  nothing is fetched from a CDN at runtime.
  [`client/vendor/README.md`](../client/vendor/README.md)

## Voice

- **Remote ears**: `server/asr_server.py` — Faster-Whisper as an
  OpenAI-compatible HTTP server on a GPU box (Docker or `uv`), optional
  bearer key, `asr: remote` on the PC; local Whisper and Groq kept.
- **Hub-side TTS (Breeze-TTS-2)**: the hub speaks every reply in each
  persona's voice (design from a description, clone from a clip, lock);
  clients just download the WAV — no TTS keys on the PC. Docker stack in
  `tts/` (NVIDIA/AMD/Intel/CPU), plus the older PC-side modes
  (`openai` / `sovits` / `fish` / `none`). [docs/VOICE.md](VOICE.md)
- **Always-listen mode**: hands-free VAD (Silero + Whisper evidence, no
  phrase blocklist), echo guard with rolling pre-roll, remembered per
  user; push-to-talk still available side by side. (tests/test_listen_mode.py)
- **Name gate**: "only when I say her name" — utterances reach her only
  when they address the active persona (name/keywords/aliases, fuzzy,
  after the name corrections), optional auto persona-switch on another
  name, follow-up window; never bites push-to-talk, typing or dictation.
  (riko/name_gate.py, tests/test_listen_name_gate.py)
- **Hearing names right**: Whisper vocabulary prompt from persona names +
  `asr.vocabulary`, exact `asr.corrections` fixups, conservative fuzzy
  pass, and a `voice: true` safety-net note so she silently assumes the
  closest name.
- **Speech-text rules**: the voice gets a cleaned *speech text* of the
  reply — never URLs, bare domains or emails ("GitHub", "their email
  address"), punctuation seams tidy, and every prose paragraph is spoken,
  not just the first. (docs/VOICE.md, docs/API.md)

## Embodiment

- **Expressive avatar**: procedural motion layer (blinks, breathing,
  saccades, glances, pointer-following head-only gaze), vowel-shape lip
  sync, keyframed gestures (wave, think, listen, stretch, fidget, nod…),
  a pure state-machine director with cooldowns, VRMA clip drop-ins
  (`client/animations/*.vrma`) re-targeted per persona, and emotion tags
  from the hub driving face + gesture.
  [docs/ROUNDS.md](ROUNDS.md), [docs/ANIMATION.md](ANIMATION.md),
  `client/js/`
- **She knows her body**: clients register body capabilities (playable
  actions/expressions) with the hub; asking "wave at me" in your own
  words performs it, timed into the spoken sentence; the LLM is prompted
  that action tags are the only way the body moves, with asked-for
  fallback and claimed-action guards. [docs/BODY.md](BODY.md)
- **Walking**: walk cycle + director walk state driving the roam strip
  (see Desktop app). [docs/WALK_MEMORY.md](WALK_MEMORY.md)
- **Touch & posing**: press-and-hold to grab a limb (two-bone IK within
  joint limits, spring-back), pats/pokes/tickles recognised from click
  patterns with instant on-page reactions plus optional rate-limited
  spoken lines, drag-her-hips-to-the-monitor-edge and she sits there with
  dangling legs (depth-adaptive leg flex), all arbitration-tested.
  [docs/TOUCH.md](TOUCH.md)
- **She authors her own animations**: "invent a victory dance" →
  `create_animation` validates/clamps her keyframes, previews the draft
  live, "keep it" keeps it; kept moves play like built-ins
  (`[act:name]`); animations are scoped per persona *and* per avatar
  model with `adapt_animation`; hold semantics for keyframes, a measured
  pose cookbook, web Animations tab (Preview/Keep/Rename/Delete), five
  hub tools, schema v10 table.
  [docs/SELF_ANIMATION.md](SELF_ANIMATION.md)

## Tools

- **Tool loop in the hub** with hub tools (`web_search`, `fetch_page`,
  `get_time`, `calculator`, `weather`, `remember`/`recall`/`forget_fact`,
  `timer`/`list_timers`) and **client tools** delivered over a registered
  client channel (browser, clipboard, desktop, typing, music…); per-tool
  permissions (`always` / `ask me` / `never`), approval bubbles, tool
  log, progress events, step/time budget.
  [docs/TOOLS.md](TOOLS.md), [docs/API.md](API.md)
- **MCP client on the hub**: stdio and HTTP servers from the web page,
  tools surfaced as `mcp_<server>__<tool>` with the same permissions;
  status/restart endpoints; `uvx`/`npx` work in the image. Built-in
  **Home Assistant** server via HA's official MCP integration
  (`RIKO_HA_URL` + `RIKO_HA_TOKEN`), only the entities you expose.
  [`hub/README.md`](../hub/README.md), [docs/API.md](API.md)
- **Web search via SearXNG** (`RIKO_SEARXNG_URL`) + `fetch_page` with an
  SSRF guard (`RIKO_ALLOW_PRIVATE_FETCH` to relax).
- **Weather + Location**: built-in Open-Meteo tool, place-name geocoding,
  unit presets and per-quantity overrides, 10-min cache, spoken numbers.
- **Timers**: one-shot scheduled turns ("in 10 minutes…"), hub-scheduled
  and re-entering the same chat with context; "unless I get to it"
  cancellation via chat-read reports; list/cancel from any client.
  [docs/TIMERS.md](TIMERS.md)
- **Desktop awareness** (off by default, privacy-gated): open windows,
  focused window, monitors, installed apps, `open_app`,
  `focus_window`, `switch_workspace` across **Hyprland (hyprlang and
  0.55+ Lua configs), Sway, KDE and GNOME** (bundled Shell extension);
  password-manager/private windows never named; toggling re-registers
  tools live. [docs/DESKTOP_SENSE.md](DESKTOP_SENSE.md)
- **Compositor-safety rules**: exactly one dispatch per action, results
  verified against live compositor state before claiming done, refusals
  for WM/session/power levers, empty-workspace switches reported
  unambiguously. [docs/DESKTOP_SENSE.md](DESKTOP_SENSE.md)
- **Firefox extension** (`browser-ext/`, "Riko Helper", MV3): reads and
  drives your real Firefox (tabs per window, active tab, focus order,
  open/focus/close tabs) through the desktop app over a loopback bridge
  with risk-gated approvals; headless e2e check against real Firefox.
  [docs/ROUNDS.md](ROUNDS.md), `browser-ext/`
- **Typing & dictation via ydotool**: keystrokes into any app; dictation
  mode pins the focused window and pauses if focus moves; **Enter in a
  terminal always needs a run token** issued for the exact typed line
  (never a generic approval); Ctrl+C/D/Z gated separately.
  [docs/TYPING.md](TYPING.md)
- **App editing**: Obsidian note create/open/read/edit through the Local
  REST API plugin, Kate tab/file editing, `find_file`.
  [docs/TYPING.md](TYPING.md)
- **`run_command` with a policy**: commands run on your PC with output
  back to her; allowlisted read-only commands auto-run, everything else
  shows the exact command + cwd in an approval; never elevated, never a
  compositor/power/disk destroyer, never interactive; scrubbed env,
  timeouts, output caps, secret masking; ~30 natural-language **recipes**
  (`command_recipes`) map your phrasings to correct commands.
  [docs/COMMANDS.md](COMMANDS.md)
- **Action guards**: promised/claimed-action guards catch "Done — I did
  it" turns with no tool call; tool-talk guard; repeat and
  complaint-read handling.

## Music

- **Spotify over the Web API** from the hub: play/control/now-playing/
  devices/transfer/like/playlists tools, OAuth via loopback callback run
  by the desktop app (token stored hub-side, write-only), playback of the
  owner's own playlists and Liked Songs by name.
  [docs/SPOTIFY_NOTES.md](SPOTIFY_NOTES.md)
- **Spotify Soloist as the managed player**: Riko starts/stops/states
  Soloist (Spotify's official headless Connect player) from the tray,
  auto-updates the binary from Spotify's official server before 90-day
  build expiry, clamps cache to Soloist's rules, refuses off-host CDN
  redirects; your API key lives only in `~/.config/riko/soloist.key`
  (0600, owner-written, never logged or sent anywhere); ducking while she
  speaks. Soloist is downloaded by each user — not redistributed.
  [docs/SOLOIST.md](SOLOIST.md)
- **Now-playing ticker**: a bottom bar showing the current track with a
  play/pause button. (client/js/ticker.js, tests/test_music_ticker.py)

## Web config page

- One page on the hub for everything: personas (prompt, avatar upload,
  greeting, keywords, appearance, emotes), LLM settings with
  source-aware defaults and a test button, memory tab, voice panels
  (design/clone/lock + preview), tools section (enable, limits,
  permissions, SearXNG, log, online clients), MCP panel, timers,
  animations tab, touch settings card, chat with emotion/memory/timer
  chips and audio playback, live persona swap.
  [docs/API.md](API.md)

## Testing

- **2,300+ pytest tests** (plus 170 node tests for the avatar page) (`uv run pytest`): hub API/store/migrations,
  tools, timers, memory, voice, packaging/env drift, desktop loopback,
  listen modes, name gate, touch arbitration, typing/dictation, vendor
  integrity, backups.
- **Node suites** (`node --test client/js/`, `browser-ext/test/`):
  director, gestures, IK, keyframes, pose, walk, ticker, touch, plus
  extension background/content tests.
- **Headless visual QA harness**: `client/tools/capture_frames.py`,
  `actions_check.py`, `listen_timing_check.py` and the capture harness
  render the real avatar and assert poses, action timing, grab/sit/pat
  scenarios and re-listen gaps. `browser-ext/scripts/headless_check.py`
  drives the extension in a real Firefox via web-ext.

## Docs

- Every feature ships with a contract doc that children implement
  literally: [API.md](API.md), [DESIGN.md](DESIGN.md),
  [ROUNDS.md](ROUNDS.md), [TOOLS.md](TOOLS.md), [VOICE.md](VOICE.md),
  [BODY.md](BODY.md), [ANIMATION.md](ANIMATION.md),
  [WALK_MEMORY.md](WALK_MEMORY.md), [TIMERS.md](TIMERS.md),
  [TOUCH.md](TOUCH.md), [SELF_ANIMATION.md](SELF_ANIMATION.md),
  [TYPING.md](TYPING.md), [COMMANDS.md](COMMANDS.md),
  [DESKTOP_SENSE.md](DESKTOP_SENSE.md), [SPOTIFY_NOTES.md](SPOTIFY_NOTES.md),
  [SOLOIST.md](SOLOIST.md), [BACKUPS.md](BACKUPS.md),
  [BACKLOG.md](BACKLOG.md) (feature map), plus `hub/README.md`,
  `tts/README.md` and `client/vendor/README.md`.
