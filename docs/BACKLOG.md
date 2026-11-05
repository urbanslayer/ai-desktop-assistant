# Backlog

Agreed with the owner on 2026-11-04. The order is the suggested order; the owner decides what goes next.

## Done (2026-11-04)

- Walking strip (`desktop.roam: strip`), long-term memory, head-only gaze fix, always-listen mode,
  extension link-tab probe (0.1.1 signed), Home Assistant control via HA's MCP Server
  (`RIKO_HA_URL`/`RIKO_HA_TOKEN` on eve; this replaced the old "HA uses Riko's ASR via
  wyoming_openai" idea, which the owner chose not to pursue).

## Planned

1. **Android app** (owner request). A native client against docs/API.md: chat, voice (push-to-talk and
   always-listen, like the desktop), the hub's audio for her voice, persona switching, events long-poll,
   body capabilities if it shows the avatar, memory chips, approvals.
   - **Connectivity: embedded Tailscale ("Tailscale for apps")**, so the app always reaches the home
     network (hub `192.0.2.51:8002`) without the system-wide Tailscale VPN app. Use the embeddable
     userspace Tailscale library (libtailscale/tsnet, built for Android via gomobile). It joins the
     owner's tailnet as its own node (auth key or one-time login, stored in Android Keystore), and the
     hub is reached through a tailnet subnet route or a node on eve.
   - **Only start Tailscale when off the home Wi-Fi.** On every network change: if the hub's LAN
     address answers (`GET /api/health`, ~1 s timeout), talk directly and keep Tailscale stopped;
     otherwise start the embedded node and use the tailnet path. A home-SSID check is an optional
     shortcut: on Android, reading the SSID needs location permission, so the reachability probe is
     the primary signal.
   - Needs from the owner, when this starts: the tailnet (Headscale or tailscale.com?), whether eve or
     another box advertises the `192.0.2.0/24` route, and target Android version / device.
2. **Music: Spotify and Tidal** (owner request). She can play music on request ("play some lo-fi on
   Spotify"), with a small "now playing" popup by her showing the track, artist, cover art and
   source (Spotify/Tidal, which device), plus play/pause/skip. Later, the Android app streams too.
   Constraints to verify against current terms when this starts:
   - **Spotify:** playback control through the Web API needs **Spotify Premium**, and the Web API only
     controls a Spotify Connect device; it doesn't stream audio itself. Desktop options:
     (a) the **Web Playback SDK** inside her page, which makes her window a Connect device (needs Widevine
     DRM, which QtWebEngine lacks by default, so a risk);
     (b) control the official Spotify desktop app as a Connect device (simplest, no audio in-app);
     (c) a local Connect receiver such as librespot/spotifyd (unofficial, a ToS grey area).
     Android: the **Spotify App Remote SDK** drives the installed Spotify app.
   - **spotify-tui analysis** (owner pointer, 2026-11-05; github.com/Rigellute/spotify-tui):
     - **How it works:** a Rust TUI that uses the official **Spotify Web API** through the user's own app
       on the Spotify Developer Dashboard. It's set up with a client ID, a redirect URI
       (`http://localhost:8888/callback`) and an OAuth consent screen, and that dashboard-app pattern is the
       right one for us.
     - **Its limits:**
       - It is **control-only**: its README says the Web API "doesn't handle streaming itself". You need
         the official Spotify client or spotifyd running as the device it plays on, and **Premium** to play.
       - It is **unmaintained**: last release v0.25.0 in 2021-08, last push 2024-04, Rust. So don't embed
         it; it isn't usable on Android anyway.
       - The maintained successor, `aome510/spotify_player` (active, ~7k stars), embeds librespot for real
         streaming. That makes it unofficial streaming, the ToS grey area noted above.
     - **Plan derived from it:**
       - **Hub-owned Spotify integration:** the owner creates one Developer Dashboard app. The hub does the
         OAuth Authorization Code + PKCE flow (redirect to the hub's own `/api/spotify/callback`, reached
         from the web page) and keeps the refresh token server-side, write-only. Hub tools:
         search/play/queue/pause/skip/volume/now-playing/**list devices**/**transfer playback**.
       - **Every client gets music control through the hub for free:** desktop, web page, Android.
       - **Where the sound comes out = any Spotify Connect device:**
         - the official Spotify app on the PC or phone;
         - the hub can **transfer playback** to the owner's **Amazon Echo devices**. With Spotify linked in
           the Alexa app, Echos appear as Spotify Connect devices ("play it on the kitchen Echo"), so no app
           has to run on the Echo at all.
       - **Android app:** control via the hub. For playing on the phone itself, start or hand off to the
         official Spotify app (App Remote SDK, or a Connect transfer to the phone's own Spotify device).
       - **Fire OS 5 (older Echo devices):**
         - Fire OS 5 is Android 5.1 (API 22), found on screened Echos (Echo Show 1st/2nd gen, Echo Spot).
         - Plain Echo speakers don't run Android apps.
         - Stock screened Echos don't allow sideloading APKs, so "the Android app on an Echo" likely needs
           a developer/rooted device. Treat it as stretch.
         - The Connect-transfer route covers playback on every Echo regardless.
         - If the Android app should still install on API 22, set `minSdk 22` from the start and check that
           each dependency supports it (gomobile/libtailscale, the Spotify App Remote SDK, the audio/VAD libs).
           Otherwise target API 26+.
     - **Verify when this starts:** the Developer Dashboard's current development-mode rules (user cap,
       Premium requirements for the app owner, quota/extension policy) and current Web API endpoint
       availability. Spotify has tightened these over 2024–2026.
   - **spotifyd as the desktop playback device** (owner pointer, 2026-11-05; github.com/Spotifyd/spotifyd):
     - **What it is:** a lightweight Rust daemon built on librespot that **streams audio itself** and
       shows up as a **Spotify Connect device**. That makes it the missing playback half: the hub's Web API
       tools control it like any other device ("play lo-fi" → transfer/play on the device "Riko").
     - **Health:**
       - spotifyd v0.4.2 (2025-11-19), last push 2026-05, ~10.8k stars, GPL-3.0 (run as a separate
         process, not linked into our code, so no licence entanglement).
       - librespot v0.8.0 (2025-11-10), pushed 2026-10.
       - Packaged in Arch `extra` (spotifyd 0.4.2-1).
       - Release builds for Linux x86_64/aarch64/armv7 and macOS. **No Android or Windows builds.**
     - **Requirements and caveats:**
       - **Premium** is required.
       - librespot is an **unofficial** Spotify client, so it's a ToS grey area. It's widely used, but this
         is the owner's call, which they've effectively made by pointing at it.
       - Audio goes to PulseAudio/PipeWire, so her music and her voice share the speakers. The desktop
         should duck spotifyd while she speaks: through spotifyd's MPRIS volume over D-Bus, or a Web API
         volume set on that device.
     - **Desktop design:**
       - `desktop.music.spotifyd: off | managed | external`:
         - `managed`: desktop.py starts and stops a spotifyd child process, with device name = persona
           name or "Riko" and the PipeWire backend. Its credentials are cached by spotifyd's own OAuth
           login (`spotifyd authenticate`, a one-time browser flow) under ~/.cache/spotifyd, never by Riko.
         - `external`: the owner runs it as a systemd user service (`systemctl --user enable --now
           spotifyd`) and the desktop only targets it by device name.
       - The now-playing popup reads state from the hub (Web API `currently-playing`) or locally from
         spotifyd's MPRIS. Prefer MPRIS on desktop: instant, no polling quota.
     - **Elsewhere:**
       - **eve:** no speakers, so not useful there.
       - **Android:** use the official Spotify app as the Connect device (spotifyd has no Android build;
         a librespot Android port would be further unofficial work).
       - **Echos:** use their built-in Connect support. spotifyd isn't needed there.
   - **Tidal:** the official Tidal developer platform / player SDKs (Web and Android), which need
     developer access approval; otherwise control via Tidal Connect.
   - Shape: hub tools (`music_play(query, service?)`, `music_control(pause|resume|next|prev|volume)`,
     `now_playing()`) with OAuth tokens stored server-side (write-only, refresh handled by the hub);
     the desktop shows the popup from a `now_playing` event; she ducks or pauses music while she speaks.
3. **Calling other models** (OpenAI, Claude, Gemini, OpenRouter): docs/TOOLS.md §C. **Plan only; do not
   implement until the owner says go.**
4. **Optional: Riko as Home Assistant's conversation agent** for HA's own voice devices (Voice PE, phone
   app). Offered, not chosen. Would need an OpenAI-compatible chat endpoint on the hub.
5. **Optional: "only when I say her name"** filter for always-listen mode.

## Quick wins

6. **Works offline**: bundle three.js, three-vrm and three-vrm-animation under `client/vendor/`
   (pinned versions, licence files) instead of loading them from the jsdelivr CDN.
7. **Hub backups**: nightly copy of `~/AI/Docker/riko-hub/data` (and `breeze-tts/data/voices`, the ASR
   model is re-downloadable) to the owner's Duplicati on Nemu, or a simple dated tarball with rotation. Owner to
   confirm the target.

## Owner's steps

8. Fill in each persona's **Appearance** on the hub web page.
9. Push / open a PR: everything is on the local branch `feat/local-desktop`, nothing is pushed yet.

## Loose ends

10. The status pill text during tool use is a stopgap: give the page a first-class `status(text=…)`.
11. The thinking-pose fingers only curl on rigs that have weighted finger bones (the sample avatar doesn't).
12. Test-run warnings (MCP SDK ping deprecation, FastAPI `on_event`); silence or migrate.
