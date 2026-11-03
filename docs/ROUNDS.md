# Rounds 1–2 contract: expressive avatar, emotion tags, tool loop, client tools, Firefox, MCP

Binding for children. Builds on docs/DESIGN.md, docs/VOICE.md, docs/ANIMATION.md, docs/TOOLS.md and the
current docs/API.md. Report contract problems; don't edit this file.

## Round 1 (parallel)

### R1-A "Expressive avatar" (client side)

Owns: `client/**` (index.html, new `client/js/*.js`, new `client/animations/`), `riko/viewer.py`,
`riko/session.py` (emotion passthrough only), `tests/test_viewer.py`, `tests/test_session.py`,
`tests/test_client_js.py` (new), and the README section about her look/animations.
Must not touch: `hub/**`, `tests/test_hub_*`, `docs/API.md`, `desktop.py` (except if strictly needed for a
new UI event; report it), `riko/hub_client.py`.

- Layers per docs/ANIMATION.md: procedural (blink incl. double blink, breathing, eye saccades via
  `vrm.lookAt`, head following gaze, idle glances every 20–40 s, gaze on camera while listening/speaking,
  follow the pointer inside the window, vowel lip sync aa/ih/ou/ee/oh from band energies) and gestures
  (wave, think = hand to chin looping while thinking, listen = lean + head tilt, talk gestures while speaking,
  idle stretch/look-around/fidget every 1–3 min, emotion gestures nod/shake/laugh/pout where cheap).
- Gesture source: owner chose **free VRoid/pixiv motion packs only, bundled**. Check whether pixiv's official VRMA
  motion pack can be downloaded without a login and redistributed. If yes, bundle the needed clips in
  `client/animations/` with their licence file. If not, author the gestures yourself as keyframed bone poses in
  code (`client/js/gestures.js`, normalized humanoid bone rotations, so every VRM works), and ship a VRMA
  loader (`@pixiv/three-vrm-animation` 3.5.x via the same CDN importmap style the page already uses) that
  picks up any `.vrma` files dropped into `client/animations/` by name (wave.vrma, think.vrma, …) instead of the
  built-ins. Document exactly where to download the pixiv pack. Report which way you went.
- An animation **director** (`client/js/director.js`, pure logic, no three.js imports) is a state machine fed by
  the UI events the page already gets (`status` listening/hearing/thinking/speaking/idle, `persona`, `say`,
  `heard`, `error`) plus a new `emotion` field. It decides which gesture/expression plays: no interrupting
  a gesture with another, a cooldown per gesture, wave on persona switch / app start / after 10+ min idle.
  Unit-test it with `node --test` (tests/test_client_js.py runs `node --test client/js/` and fails if node is
  missing only with a skip).
- **Emotion:** `ChatResult.emotion` (from the hub, see R1-B) is passed by `riko/session.py` into the
  viewer's `say` event as `emotion` (and for switch greetings: `happy`). The page maps it to VRM expressions
  `happy/angry/sad/relaxed/surprised` (teasing → happy 0.6 + a head tilt, annoyed → angry 0.5,
  embarrassed → happy 0.3 + look away, thinking → neutral + think gesture, neutral → none), blended in and
  out over the line. A hub without `emotion` → neutral.
- Keep all existing behaviour (drag/zoom/turn, bubble, shortcuts, persona VRM hot-swap: gestures must
  re-target to the new VRM after a swap).
- No windowed launches or screenshots (no permission). Verification: `node --check` on every script,
  `node --test`, plus a headless page smoke test if you can (e.g. load the page in a headless browser with a
  stub /next and confirm no console errors). If a real browser isn't available headless, say so.

### R1-B "Hub brain: emotion + tools" (hub side)

Owns: `hub/**`, `tests/test_hub_*.py`, `docs/API.md`. Must not touch client/, riko/, desktop.py, README.md.

**Emotion tags**
- Fixed set: `neutral, happy, teasing, annoyed, sad, surprised, thinking, embarrassed`.
- Persona gains `emotes: bool` (default true; migration for old DBs).
- When true, the hub's system prompt adds a short instruction to start every reply with one tag like
  `[teasing]`. The hub strips the tag (also tolerating `(teasing)`, a missing tag or an unknown tag →
  `neutral`; never leave a tag in the stored or spoken text) and stores `emotion` on the assistant message.
- `ChatResult.emotion` and `Message.emotion` (null for user messages).
- Breeze direction: when synthesizing a message, append a delivery hint per emotion to the voice instructions,
  e.g. teasing → "playful, teasing tone", annoyed → "irritated, clipped". The audio cache key includes the
  emotion (it's fixed per message anyway).
- Web page: an `emotes` toggle per persona, and the emotion shown next to replies in History/Chat.

**Tool loop** (docs/TOOLS.md A; delegation to other models in section C is NOT built)
- OpenAI function calling against the configured LLM (verified: qwen4exp on vLLM returns `tool_calls`).
- Loop: up to `max_steps` (8) model calls and `max_seconds` (90). Independent tool calls run in parallel
  threads. Tool results go back as `role: tool` messages, wrapped as untrusted data: a short prefix telling the
  model it's web/tool data, not instructions. Results are capped (default 12 000 chars per result).
- The final reply follows the emotion rules above. Stored history keeps only the user message and the final
  assistant reply (plus a compact `tools_used` list on the assistant message: `[{tool, label}]`); the full trace
  goes to the tool log.
- If the LLM rejects `tools` (400), retry once without them, mark it in `/api/tools/status`, and carry on as before.
- System prompt addition when tools are on: what the tools are for, "keep the spoken answer short (1–3
  sentences); put links and details after a blank line", and the current date/time.

**Hub tools** (built-in, `hub/tools/`)
- `web_search(query, max_results=5)` → SearXNG `GET {searxng_url}/search?q=…&format=json` → `[{title, url,
  snippet}]`. Default URL from env `RIKO_SEARXNG_URL` (owner's: `http://192.0.2.101:8989`).
- `fetch_page(url)` → readable text of an http(s) page (stdlib html.parser based main-text extraction or a
  small maintained dependency; strip scripts/styles/nav; title + text + list of links capped). Only http/https;
  refuse private/loopback IPs unless `allow_private_fetch` is set (SSRF guard); 10 MB / 20 s limits.
- `get_time(timezone?)` → ISO time and weekday.

**Permissions and settings**
- `PUT/GET /api/settings` gain `tools: {enabled: true, max_steps, max_seconds, searxng_url,
  allow_private_fetch: false, permissions: {"<tool>": "always"|"ask"|"never"}}` with the existing
  blank=default semantics.
- Defaults: hub tools `always`; client tools `always` for read-only ones and `ask` for risky ones (the client
  declares `risk: "read"|"act"|"danger"`; act/danger default to `ask`).
- `never` tools aren't offered to the model at all.
- `GET /api/tools` lists all tools (hub + online client tools) with their source, risk and permission.
- `GET /api/tools/log?limit=50` gives the recent calls: time, persona, tool, arguments (truncated), ok/error,
  duration, approval outcome.

**Events (progress for every client)**
- `GET /api/events?after=<seq>&timeout=<s ≤ 30>` is a long-poll returning `{"events": [{seq, time, type, …}],
  "last": seq}`. It returns immediately if anything is newer than `after`. The ring buffer holds the last 500 events.
- Types: `turn_started {persona, source}`, `tool_started {call_id, tool, label}` (label is a short human
  phrase, e.g. "searching the web for 'tokyo weather'"), `tool_finished {call_id, tool, ok, ms}`,
  `approval_needed {request_id, client_id, tool, summary}`, `turn_finished {persona, message_id, emotion}`,
  `state_changed {version}`.

**Client-tool channel** (server side in R1-B; the desktop side comes in R2-F)
- `POST /api/clients/register {client_id, name, kind:"desktop"|"android"|…, tools:[{name, description,
  parameters (JSON schema), risk}]}` → `{ok, heartbeat_s: 30}`. The client re-registers (or `POST
  /api/clients/{id}/heartbeat`) every 30 s and is offline after 75 s without one.
- `GET /api/clients/{id}/requests?timeout=25` is a long-poll returning `{"requests": [{request_id, tool,
  arguments, needs_approval: bool, summary}]}`.
- `POST /api/clients/{id}/results {request_id, ok, result?: any, error?: str, approved?: bool}`.
- The model sees online client tools as `<kind>__<name>` (e.g. `desktop__open_url`; function names
  `^[a-zA-Z0-9_-]{1,64}$`). The hub waits for the result up to `max_seconds`; approval waits up to 120 s, then
  the result is "not approved".
- `GET /api/clients` lists online clients and their tools.
- Tests use a fake client thread.

**Verification:** the full suite stays green. Real check against the real LLM and the real SearXNG: "what's the
weather in Tokyo today?" produces a web_search + reply; "summarise <some page>" produces fetch_page; events seen
via the long-poll; a fake desktop client with `open_url` gets the request. Don't touch the deployed hub (the
reviewer redeploys).

## Round 2 (parallel, starts right after round 1 is merged)

### R2-F "Firefox helper + desktop client tools"

Owns: `browser-ext/**` (new), `riko/client_tools.py` (new), `riko/hub_client.py` (client channel),
`desktop.py` (approval UI, tray), `riko/session.py`, `bin/riko-ctl`, `character_config.yaml`, README tools
section, non-hub tests.

- Desktop client registers with the hub (R1-B channel) and runs client tools: `open_url` (xdg-open, http(s)
  only, risk read), `clipboard_read` (risk act, ask), `clipboard_write` (risk act), `notify` (risk read), and
  the `browser_*` tools relayed to the Firefox extension.
- **Approvals:** a tray notification + a bubble on the avatar ("Can I … ? Enter = yes, Esc = no", plus
  `riko-ctl approve|deny`). A timeout means deny.
- **Firefox extension "Riko Helper"** (WebExtension, Firefox MV3/MV2 as needed, `browser_specific_settings.gecko.id`):
  - Connects to the desktop app on `ws://127.0.0.1:<port>`. The app runs that local WebSocket server.
    Authentication is a per-install token shown in the app and pasted once into the extension options.
  - Tools (risk in brackets):
    - `browser_tabs` [read]
    - `browser_open(url, new_tab)` [read]
    - `browser_read(tab?)` → title, URL, readable text, numbered interactive elements [read]
    - `browser_click(n)` [act]
    - `browser_type(n, text, submit?)` [act; `submit` → danger]
    - `browser_select(n, value)` [act]
    - `browser_scroll` [read]
    - `browser_back` [read]
    - `browser_screenshot` [read]
    - `browser_close(tab)` [act]
  - Anything that submits a form, or clicks an element whose label/form looks like
    send/buy/pay/order/post/publish/delete/remove/confirm/sign in, is upgraded to `danger` and always asks,
    whatever the setting.
  - In-page banner while she's acting, a toolbar kill switch, a per-site allow list, and no access to
    password fields' values (she can't read or type into `type=password`).
  - Build with `web-ext lint`/`web-ext build`. The README explains unlisted AMO signing (`web-ext sign
    --channel unlisted`, needs the owner's AMO API key) and temporary loading for testing.
- Tests: Python side against a fake hub and a fake extension WebSocket client; the extension's pure logic
  (risk classifier, snapshot numbering) with `node --test`.
- No windowed launches or screenshots of the owner's desktop.

### R2-M "MCP plug-ins on the hub"

Owns: `hub/**`, `tests/test_hub_*`, `docs/API.md`.

- The hub becomes an MCP client using the official `mcp` Python SDK. Servers are configured in settings (web page
  + `PUT /api/settings` `mcp: {servers: [{name, transport: "stdio"|"http", command?, args?, env? (write-only
  values), url?, headers? (write-only), enabled, tool_permissions?}]}`).
- Their tools appear to the model as `mcp_<server>__<tool>` with risk `act` unless the server marks a tool as
  read-only (`readOnlyHint`). They go through the same permissions, log and events.
- Connection lifecycle: start on enable, restart with backoff, status in `GET /api/mcp/status`, timeouts.
  stdio servers run inside the hub container (document what's installable there; the Dockerfile gets `node` +
  `npx` and `uv`/`uvx` so common MCP servers can run).
- Tests: a tiny in-repo MCP server fixture (stdio) exposing two tools, used end to end through the chat
  loop with a fake LLM.
- Real check: the official "fetch" or "time" reference server via uvx, if reachable.

Round 2 ownership note: R2-F and R2-M don't share files. R2-M must not edit the client-tool channel code
from R1-B beyond what MCP needs; if it must, keep it to `hub/tools/registry` interfaces.
