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
