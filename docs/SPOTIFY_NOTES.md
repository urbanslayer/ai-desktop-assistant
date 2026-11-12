# Spotify: verified constraints (checked 2026-11-10 against developer.spotify.com)

Owner decisions (2026-11-10): has **Premium**; will create the Developer Dashboard app when given
steps; desktop playback via **spotifyd managed by Riko** (device name "Riko"); **Spotify first**,
Tidal later once developer access is approved.

## Rules that change the design (verified 2026-11-10)

- **Redirect URIs** (`/documentation/web-api/concepts/redirect_uri`): HTTPS required, **except**
  loopback IP literals (`http://127.0.0.1:PORT/...`, `http://[::1]:PORT/...`). **`localhost` is not
  allowed.** A loopback URI may be registered **without a port** and the port added in the
  authorization request.
  → The OAuth callback CANNOT be the hub's `http://192.0.2.51:8002/...` (plain HTTP, not loopback).
  **Design:** the **desktop app** runs the browser step and receives
  `http://127.0.0.1:8911/callback` (fixed port, configurable), then hands the code to the hub, which
  exchanges and stores the refresh token (write-only). The web page can also do it purely client-side
  with PKCE against its own `http://127.0.0.1:PORT` listener only if a local helper exists; simplest
  is: connect from the desktop app, and every other client then uses the hub's stored token.
  For a headless hub-only setup, the page shows the URL and the owner pastes back the redirected
  `code=` value (paste-back fallback).
- **Development mode** (`/concepts/quota-modes`, Feb-2026 migration guide): app **owner must hold
  Premium** (app stops if it lapses); **5 users** per app max (fine: single user, no allowlist needed
  for yourself); Client IDs per developer is 25 as of July 2026; exceeding quota returns HTTP 429
  `QUOTA_EXCEEDED` (must back off); extended quota is companies-only (250k MAU), so we stay in
  development mode forever.
- **Endpoints removed for development-mode apps** created after 2026-02-11 (existing apps were
  migrated 2026-03-09 — a brand-new app gets the new rules):
  - `GET /search`: **`limit` max is 10** (default 5) — paginate with `offset` for more.
  - Batch fetches gone: `GET /tracks?ids=`, `/albums`, `/artists`, `/episodes`, `/shows`,
    `/audiobooks`, `/chapters` → fetch one by one.
  - Library: `PUT/DELETE /me/tracks|albums|...` and `/me/following` → generic
    `PUT/DELETE /me/library` (takes URIs); `GET /me/library/contains`.
  - Playlists: `/tracks` paths renamed to `/items`; playlist **items are only returned for playlists
    the user owns/collaborates on**.
  - Gone: `/browse/new-releases`, `/browse/categories*`, `/artists/{id}/top-tracks`, `/markets`,
    `GET /users/{id}`, `/users/{id}/playlists`.
  - Fields gone from responses: `popularity` (track/album/artist), `followers` (artist/user),
    `country`/`email`/`explicit_content`/`product` on `GET /me`, album `label`, `available_markets`
    (mostly), track `linked_from`. **`product` is gone, so Premium cannot be confirmed via the API**
    — treat a 403/404 on player calls as "is Premium still active?".
  - The **full Player API is still available** (play/pause/next/prev/seek/volume/shuffle/repeat,
    devices, transfer, currently-playing, recently-played, queue) — everything this feature needs.

## Managed spotifyd start (owner PC, verified 2026-11-10)

The owner's live failure was `spotifyd exited right away (code 2)` — two
independent causes, both fixed in `riko/music/spotifyd.py`:

- **`--backend pipewire` is not a spotifyd backend.** Arch spotifyd 0.4.2
  accepts exactly `rodio|alsa|pulseaudio|pipe|subprocess`; clap printed its
  usage error and exited 2. PipeWire serves the PulseAudio protocol, so the
  default (and the config value) is now `pulseaudio`; a leftover
  `pipewire` is mapped to it with a one-line log note.
- **The owner's `~/.config/spotifyd/spotifyd.conf` predates Riko** (ncspot
  days): `cache_path = "cache_directory"` is RELATIVE, so
  `spotifyd authenticate` run from `~` wrote credentials to
  `~/cache_directory/oauth/credentials.json`, plus a junk
  `on_song_change_hook` and an old username. Riko never reads that file:
  managed mode writes its own minimal conf to `~/.config/riko/spotifyd.conf`
  (0600, no hooks, `use_mpris = true`, `device_type = "computer"`,
  bitrate 320) and passes `--config-path` + absolute `--cache-path`
  (`~/.cache/riko/spotifyd`, config override `desktop.music.spotifyd_cache`)
  with `cwd` = the cache dir, so no relative path can scatter files again.
- **Credential migration is copy-only.** If `<cache>/oauth/credentials.json`
  is missing, Riko looks in `~/.cache/spotifyd/oauth`, then
  `~/cache_directory/oauth`, then `$XDG_CACHE_HOME/spotifyd/oauth` and
  COPIES the first hit (0600, log line); owner files are never moved or
  deleted. With no login anywhere Riko does not spawn and prints the exact
  one-time command: `spotifyd authenticate --cache-path ~/.cache/riko/spotifyd`.
- **Early exits report spotifyd's own output**, not a guess: stdout/stderr
  are piped into a 20-line ring buffer (daemon drain thread), and the last
  error line lands in the tray/terminal message. "Login missing" is only
  said when the output actually mentions credentials/auth.

## Consequences for the plan

- No batch-fetch shortcut when building now-playing/queue UI: fetch tracks individually (or rely on
  spotifyd's MPRIS locally, which we already prefer on desktop).
- Search results: max 10 per call; pick the top hit rather than listing many.
- "Add to library" uses `PUT /me/library` with a Spotify URI.
- Keep a strict quota discipline: cache device lists, prefer MPRIS for now-playing, no aggressive
  polling of `currently-playing`.

## Owner playlists + Liked Songs (live-bug round, 2026-11-10)

The owner asked "play my dance playlist" and got "no playlist found":
`play()` only used catalogue `GET /search`, which **does not reliably
surface the user's own (especially private) playlists**. Fixed in
`hub/spotify.py`:

- **Owner library first.** `play(kind="playlist")` matches the owner's own
  playlists (`GET /me/playlists`, paginated `limit=50` + `offset`, cached
  ~5 min in the hub and invalidated on connect/disconnect) by normalised
  name — casefold, emoji/punctuation stripped, filler words "my"/"the"/
  "playlist" dropped — with the ladder exact > startswith > contains >
  fuzzy (difflib ratio ≥ 0.8). Equally good matches: exact-case name first,
  else the first tied entry, and the tool result **names the alternatives**.
  Only when nothing matches does `play()` fall back to catalogue search.
- **`kind` guard.** A query containing the word "playlist" forces
  `kind=playlist` — the LLM otherwise sends "play my dance playlist" as a
  track search. The tool descriptions say the same ("use kind=playlist — the
  owner's library is searched first").
- **Null entries.** Spotify interleaves `null` entries in `items` lists
  (observed in playlists listings) — every items list is filtered for
  `None` before use: `/search`, `/me/playlists`, `/me/tracks`.
- **Liked Songs.** "my liked/saved songs" plays `GET /me/tracks` (first
  page, ≤ 50) as `uris` via `PUT /me/player/play` — the Web API cannot
  start the Liked Songs context directly. `shuffle: true` shuffles the
  uris client-side first.
- **Scopes grew: `playlist-read-private playlist-read-collaborative
  user-library-read` added to `SCOPES`.** Granted scopes freeze at consent
  time, so the owner must **Disconnect + Connect once** (one-time).
  `/api/spotify/status` exposes `missing_scopes`; the web Spotify card and
  every blocked owner-library call answer "reconnect Spotify to allow
  playlists / liked songs" instead of pretending the library is empty.
- **New tool `spotify_playlists(query?)`** (risk `read`, permission default
  `always`): the owner's playlist names + track counts, so "what playlists
  do I have?" has an honest, quota-cheap answer (cached list).

## 2026-11-11 live incident: silent player, "Restriction violated" 403s

Symptoms: tray player online as a Spotify Connect device, hub `play` answers
"Restriction violated" (403) on a *fresh* device, then play "succeeds" but no
audio ever reaches PulseAudio and Spotify auto-skips every ~8 s.

Diagnosis (all live-proven):
- The 403 on the first play attempt to a just-registered device is transient
  (transfer-then-play or a retry succeeds). Hub should NOT blame Premium.
- The silence is librespot failing to fetch audio decryption keys:
  `error audio key 0 1` + `Unable to load key, continuing without
  decryption` -> encrypted bytes hit the decoder -> "invalid mpeg audio
  header"/"skipping junk" thousands of times -> "Deadline expired" -> skip.
- Reproduced on BOTH Arch spotifyd 0.4.2 (old librespot) AND Arch
  librespot 0.8.0 (2026-10-28 build), fresh cache, `--cache-task never`,
  single instance, healthy network (800 Mbps, no proxies), same account.
  The official desktop app played fine ~20 min earlier on the same network.
  => Spotify-side key-service problem for librespot-style sessions (or a
  regression spanning librespot versions), NOT this machine or the config.

What to try when it's silent again (in order):
1. Just retry in 30-60 min — if Spotify's key service recovered, everything
   else (config, cache, device name) is already right.
2. `RUST_LOG=info spotifyd --config-path ~/.config/riko/spotifyd.conf
   --no-daemon` and grep for `audio key` — that line is the fingerprint.
3. Only if it persists for days: try the official desktop app as the
   Connect device (flatpak com.spotify.Client) or a newer librespot build.
Audio cache is disposable: safe to delete everything under
~/.cache/riko/spotifyd EXCEPT the `oauth/` folder.

## Resolution (2026-11-11): Spotify Soloist replaces spotifyd

The key outage persisted, so the owner chose Spotify's **official** headless
Connect player — **Spotify Soloist** — with Riko managing it (tray Start/Stop,
auto-update from Spotify's own CDN before builds expire). The full contract,
file layout and design live in **docs/SOLOIST.md**; the hub now also
transfer-retries the transient `Restriction violated` 403 once before ever
mentioning Premium (hub/spotify.py, tests/test_hub_spotify.py). spotifyd
remains selectable via `desktop.music.player: spotifyd` (legacy).

Owner steps (once, see docs/SOLOIST.md for the details):
1. https://developer.spotify.com/dashboard/soloist → generate a Soloist API
   key (Premium account).
2. In a terminal (the key never goes into chat):
   `install -D -m 600 /dev/stdin ~/.config/riko/soloist.key` → paste the key,
   Enter, Ctrl-D.
3. Restart desktop.py → tray Music → Start player (Riko installs Soloist
   herself from the official CDN on the first start).
4. On the phone's Spotify app pick "Riko" in the device list once — the
   session is stored and restored on every later start.

## Song ticker (2026-11-11, owner request)

"Small ticker at the bottom of her window showing the currently playing
song, with a play/pause button." Data is the hub's existing now-playing
state:

- **Data path:** the hub's `now_playing` event (events long-poll, deduped
  state changes) → `riko/session.py` follower thread → viewer command
  `music:state {json}` → page. A page opened mid-song gets one seed: the
  session asks `GET /api/spotify/now-playing` once right after the follower
  starts (`_seed_now_playing`, best effort — 409 stays dark). The idle
  snapshot (`playing:false, uri:""`) is forwarded too: it is exactly what
  tells the page to hide the bar.
- **Live detection (2026-11-11 live-bug round):** the poller used to sleep
  with ZERO API calls the moment state was paused/idle and only
  `wake()`d on Riko-initiated play/resume/transfer — so playback started
  from the phone (or already running across a hub restart) never produced
  an event and the ticker stayed hidden. Now:
  - *Hub idle watch:* while connected+enabled and NOT fast-polling, the
    poller fetches currently-playing every `spotify.idle_poll_s`
    (default 30 s, floor 15 s, `0` disables) — but only while someone
    cares: a desktop client is online in `clients_hub` (wired as
    `interest_hook`) OR some client fetched `GET /api/spotify/now-playing`
    within `PAGE_INTEREST_S` (120 s — a ticker page that just opened
    always seeds itself, so "page is up" ≈ "page has been asking"; an
    unwatched hub burns zero calls). Never during a quota back-off. A
    noticed play hands over to the fast poll (wake).
  - *Hub start snapshot:* `startup_snapshot()` — one immediate fetch when
    the poller thread starts (connected + no back-off), so state after a
    hub redeploy/restart is right without waiting for a wake or an idle
    period.
  - *Desktop fast path:* when WE manage the player and it is Soloist,
    `music/soloist.py::SoloistFeed` keeps a long-lived connection to its
    local WS API (`auth_state`, `playback_state`, `track_changed`,
    `playback_changed`, `context_changed`, `device_changed`,
    `position_sync` — names verified against the official WS docs, not
    guessed). On a real (uri, playing) change **while Soloist is the
    active device** it GETs `now-playing`, forwards the hub's own
    snapshot to the page as `music:state`, and posts
    `POST /api/spotify/wake` (`session.music_local` does the last step) —
    the hub stays the single source of truth for what the
    ticker paints (the WS `item` envelope is only used for change
    detection, never mapped into ticker fields; hub unreachable is the
    one local-paint fallback). `session.music_local`
    records the forwarded snapshot; a hub `now_playing` event that merely
    confirms it (same uri+playing, < 5 s later) is skipped — newer state
    always wins, no double-paint/flicker. When playback lives on another
    device the feed stays silent and the hub paths are the only painter.
  - *Latency (phone started it):* same-device (Soloist is the player):
    WS event → page ~0.3-1 s (nudge + one hub GET). Phone on another
    device / spotifyd builds: within one idle period, ≤ ~30 s.
    Already-playing across a hub restart: at hub start.
  - *Extra Spotify budget:* idle watch ≤ 1 currently-playing GET per 30 s
    while idle+watched = ≤ 120 calls/h worst case (0 when nobody is
    online/asking, 0 when `idle_poll_s: 0`); +1 call per hub start; the
    fast path itself costs 1 extra GET per playback change (the nudge POST
    costs zero). Fast-poll budget while playing is unchanged.
- **Button path:** page button → viewer `POST /control/music-control`
  `{action:"toggle"}` → desktop → `POST /api/spotify/control` → hub's
  `SpotifyHub.control` (new `toggle` branch: reads the young cache or one
  fresh call, then pause/resume — the 403 retry inside `call()` covers a
  Spotify-side "Restriction violated"). The round trip waits on the viewer
  HTTP thread, never Qt; the page flipped its icon optimistically before
  the request, so a slow hub delays only the reconcile. The response (and
  any later `now_playing`) reconciles the truth; a failed control reverts
  the flip and hides the bar until the next event proves things.
- **Page policy** (`client/js/ticker.js`, pure + node-tested): shows when
  playing or paused-with-a-track (paused shows the paused icon), hides on
  idle/nothing/disabled/error and ~10 min after a pause; text is
  `track — artists`; the marquee only engages when the painter measures
  overflow; the tray switch `music:ticker:{0,1}` overrides everything.
- **Touch safety:** the bar is `pointer-events: none` — only the button
  takes clicks. It is NOT a body-part rect (`computeParts` untouched), and
  the roam click-through mask gains the button rect as a SECOND rect via
  the bbox payload's `hit_extra`, so the strip keeps click-through
  everywhere else while the button stays clickable. Feet hit rects sit
  above the 30 px bar (capture gate: at most the rects' 6 px pad is
  nicked).
- **Bug E (live 2026-11-12):** the play/pause press never reached the hub
  (zero `POST /api/spotify/control` in the log): the desktop's floating-
  window press arbiter decided "window move" for every press outside the
  limb rects, so the button — the one interactive island OUTSIDE her body
  — swallowed it as a drag. `touch_desk.interactive_at()` now answers the
  arbiter with limb rects OR the `hit_extra` island; the optimistic flip
  + revert-on-failure page policy is untouched. Regression:
  `test_press_arbitration_passes_ticker_hits_to_the_page`,
  `test_music_control_cmd_reaches_the_hub`.
- **Config/UX:** `desktop.music.ticker: true` default; tray Music ▸
  "Show song ticker" checkbox persisted in the appstate
  (`appstate.load/save_music_ticker`) like the other tray toggles; the
  effective value is pushed to the page at every start. Bubble-family
  styling, inline SVG icons — no external fonts, offline-vendored client.
- **QA:** `capture_frames.py` scenarios `ticker` / `ticker_short` assert
  (from `metrics.tickerQA` in the measure dumps) exact text, marquee iff
  overflow, button-in-bar, and feet-rect clearance; the PNGs composite the
  ticker's real rects/text onto the canvas shot (the harness shoots the
  WebGL canvas, which cannot see DOM).

## Desktop media controls (MPRIS) (2026-11-11, owner request)

The same reconciled snapshot that paints the ticker is also exported as an
MPRIS2 player (`org.mpris.MediaPlayer2.riko`) so MPRIS consumers —
dux-shell-style Quickshell bars, GNOME, playerctl — show and control the
song beside her window. Full section: docs/SOLOIST.md, "Desktop media
controls (MPRIS)"; hub-side fields (`art_url`, the `seek` control action)
in docs/API.md.
