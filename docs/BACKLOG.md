# Backlog

Agreed with the owner on 2026-11-04. The order is the suggested order; the owner decides what goes next.

## Done (2026-11-04)

- Walking strip (`desktop.roam: strip`), long-term memory, head-only gaze fix, always-listen mode,
  extension link-tab probe (0.1.1 signed), Home Assistant control via HA's MCP Server
  (`RIKO_HA_URL`/`RIKO_HA_TOKEN` on eve; this replaced the old "HA uses Riko's ASR via
  wyoming_openai" idea, which the owner chose not to pursue).

## Done (2026-11-09)

- **"Only when I say her name" — the always-listen name gate** (was Planned 5; owner request: "when
  Furina is set to always listen, can she only respond if I say her name?"). Tray checkbox under
  *Always listen* / `riko-ctl listen name-gate on|off`, off by default and remembered across starts.
  Matches the **active persona's** hub name + keywords plus `listen.name_aliases` on the final
  transcript (after the Farina→Furina corrections), case/punctuation-insensitive, whole-word, fuzzy,
  name anywhere, `hey <name>` included — never one hard-coded name. `listen.name_gate_any_persona`
  (default on): addressing a DIFFERENT persona by name switches to her through the hub's own
  `/api/active` path first, then sends the sentence. Follow-up window (`listen.follow_up_seconds`,
  12 s) keeps nameless follow-ups flowing right after a reply; persona switches close it.
  Push-to-talk, typed text, dictation and fired timers are never gated. README "Only when I say her
  name" section; `riko/name_gate.py`, tests `tests/test_listen_name_gate.py`.

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
   **Owner decisions + verified API rules 2026-11-10 → see docs/SPOTIFY_NOTES.md** (Premium: yes;
   playback via spotifyd managed by Riko; Spotify first, Tidal after developer access; OAuth callback
   must be a `127.0.0.1` loopback — the hub's LAN HTTP URL is not a valid redirect URI; development
   mode caps search at 10 results, drops batch fetches, and `GET /me` no longer reports Premium).
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
     - **Future: our own Android build of the spotifyd idea** (owner, 2026-11-05: "a copy that does work
       on android"). Goal: the Riko Android app is itself a Spotify Connect device (plays audio in-app, no
       official Spotify app needed). Starting points, checked 2026-11-05:
       - **Rust route (preferred):** build the rust **librespot** crate (v0.8.0, actively maintained) for
         Android with `cargo-ndk` as a small JNI library: the Connect receiver, Spotify login, and an audio
         sink. librespot's built-in backends are desktop ones, so write an Android sink (Oboe/AAudio via
         the `oboe` or `cpal` crate, which support Android). Ship it inside the app with a foreground media
         service + MediaSession, so lock-screen/notification controls work, it survives backgrounding,
         and audio focus ducks it under her voice. Benefit: the same code base as spotifyd/librespot on
         desktop.
       - **JVM route:** **librespot-java** (Apache-2.0, last push 2025-11, 422 stars) runs on the JVM, and
         the old **devgianlu/librespot-android** wrapper (45 stars, last push 2023-01, no licence file)
         shows it working on Android. It's simpler to integrate in a Kotlin app, but the JVM port lags the
         Rust one on protocol changes, and the stale wrapper isn't a dependency to rely on (the missing
         licence alone rules out copying it).
       - **Fire OS 5 / API 22:** Oboe needs API 16+ (AAudio 26+, OpenSL ES fallback below), and cargo-ndk
         can target API 21+. So an in-app Connect receiver is feasible on old Fire OS hardware, which an
         installed Spotify app may not be. That's the real reason to do this rather than rely on the
         official app.
       - **Caveats:**
         - It's unofficial (same ToS grey area as spotifyd) and needs Premium.
         - Spotify periodically changes auth (e.g. login5/AP changes); upstream librespot follows them,
           so pin to a released version and track upstream.
         - It's GPL-free: librespot is MIT, so it can be embedded. Re-check at build time.
       - **Order:** after the hub Spotify integration and the basic Android app exist. Prototype as a
         standalone Android "Riko Speaker" app first, which is easier to test on an Echo Show / old Fire
         device, then fold it into the main app.
   - **Tidal:** the official Tidal developer platform / player SDKs (Web and Android), which need
     developer access approval; otherwise control via Tidal Connect.
   - Shape: hub tools (`music_play(query, service?)`, `music_control(pause|resume|next|prev|volume)`,
     `now_playing()`) with OAuth tokens stored server-side (write-only, refresh handled by the hub);
     the desktop shows the popup from a `now_playing` event; she ducks or pauses music while she speaks.
3. **Calling other models** (OpenAI, Claude, Gemini, OpenRouter): docs/TOOLS.md §C. **Plan only; do not
   implement until the owner says go.**
4. **Optional: Riko as Home Assistant's conversation agent** for HA's own voice devices (Voice PE, phone
   app). Offered, not chosen. Would need an OpenAI-compatible chat endpoint on the hub.
5. ~~**Optional: "only when I say her name"** filter for always-listen mode~~ — **DONE 2026-11-09**,
   see Done (2026-11-09) above.
6. **She creates her own animations** (owner request), for whichever avatar is active at the time.
   **Tier 1 DONE 2026-11-11 — LLM-authored keyframes per docs/SELF_ANIMATION.md** (create/keep/delete/list/adapt
   tools, model-scoped storage, preview-then-keep, web Animations section; owner live test pending). Tier 2
   (text-to-motion on eve) stays plan only.
   - **Shape:** she authors a named animation as data: keyframes of VRM humanoid bone rotations,
     expressions and timing, in the same format as the built-in gestures in `client/js/gestures.js`.
     She can then play it later through an action tag (`[act:my_bow]`) or a tool, like the built-ins.
   - **Per avatar:** animations are stored on the hub against the persona *and* the VRM model
     (by file hash), because proportions and VRM0/VRM1 axes differ (e.g. Furina's garment skinning,
     docs/BODY.md). Switching model shows only that model's set; she can "adapt" one from another
     model by retargeting the humanoid bones.
   - **How she makes them (two tiers):**
     1. *LLM-authored keyframes*: a `create_animation(name, description)` tool where the model writes
        compact keyframe JSON (a few poses plus timings). It is checked against joint limits and
        timing, then smoothed with the existing IK/motion code. This works today with no new models.
     2. *Text-to-motion on eve* (later): a motion model generates full-body motion from text, which is
        retargeted to the VRM humanoid and saved as VRMA. Candidate models to evaluate when this starts:
        MoMask, T2M-GPT, MotionGPT, HY-Motion. It needs GPU time on eve alongside the LLM and TTS.
   - **Safety and quality:** clamp to joint limits; keep the feet planted unless the animation is a
     walk; check for limbs passing through her body against the model's colliders. She previews new
     animations to the owner first, and they're kept only after the owner agrees (or a setting allows
     auto-keep). She can't overwrite the built-ins. Animations are listed, renamed and deleted in a web
     page section.
   - **Hooks that already exist:** the gesture/pose sampler (`samplePose`/`sampleGesture`,
     `mirrorPose`), `director.js`, action tags (`hub/body.py`), and the persona `appearance` field.
7. **Touch and posing: interacting with her body, not just moving her** (owner request). **DONE 2026-11-11 —
   docs/TOUCH.md** (limb grab with IK, pat/poke/tickle + hub `/api/touch` reactions, sit on the screen edge;
   owner live test pending). Original plan kept below.
   - **Sit on the screen edge:** drag her down so her legs go past the bottom of the screen, and she
     sits on the edge with her legs dangling off-screen. Lift her back up and she stands again.
     Detection: during a window drag, if the hips come within a small margin of the bottom of the
     screen, blend into a sitting pose with the hips pinned to the edge. The same could work later for
     sitting on top of a window, using the window rectangles from desktop awareness.
   - **Grab and move a limb:** click and hold her hand (or foot or head), drag to move it, and let go
     for it to spring back. Picking works by casting a ray from the cursor against the model to find
     the nearest humanoid bone (hand, lower arm, foot, head). While held, two-bone IK (`client/js/ik.js`,
     already used for gestures) follows the cursor within joint limits; on release it eases back
     through the director.
   - **Reactions:** head pats, pokes and grabs are sent to the hub as small events (e.g.
     `touch {part:'head', kind:'pat'}`), so she can react in character with an expression, a line or
     a little move. Owner-tunable, including off, and rate-limited so it doesn't spam the LLM.
   - **Click versus drag:** a short press-and-move on her body stays a window drag (the current
     behaviour). A press held still for about 250 ms on a limb grabs the limb. A modifier key (not
     Super) could force one or the other.
   - **Constraints:** works in the roam strip (she's clickable there; everything else is click-through).
     Poses must respect Furina-style garment skinning (docs/BODY.md): move limbs by their own bones,
     never the neck/chest for "look" motions. Mobile and web clients could get touch versions later.
8. **True on-device wake word** (follow-on from the name gate, done 2026-11-09). The name gate today
   is a TEXT filter: every VAD utterance is still sent to eve's ASR and gated on the finished
   transcript, so TV chatter costs a transcription round-trip even when she ignores it. A real wake
   word would decide on-device before any audio leaves the PC — candidate: **openWakeWord**
   (small ONNX models, custom persona-word training from a handful of spoken samples, runs on CPU
   next to the existing Silero VAD on the reader thread). Decide per-persona wake models vs one
   shared "hey riko", and how it composes with `listen.name_gate` (the wake word could replace the
   text gate entirely, or arm it).

## Quick wins

6. **Works offline** (done 2026-11-10): three.js 0.179.1, @pixiv/three-vrm 3.4.1 and
   @pixiv/three-vrm-animation 3.5.5 are vendored under `client/vendor/` (pinned files, MIT
   licence texts, SHA-256 manifest in `client/vendor/README.md`); the import map and every
   module import now resolve locally — `tests/test_client_vendor.py` fails if a runtime URL
   or a hash drifts back in. Still networked **on purpose**: weather (Open-Meteo), web
   search, the LLM, Spotify (internet, via the hub) and the hub/TTS/ASR servers on the LAN
   (eve). The avatar page itself, the VRM models and the fonts (system fonts, none to fetch)
   need no network.
7. **Hub backups**: nightly copy of `~/AI/Docker/riko-hub/data` (and `breeze-tts/data/voices`, the ASR
   model is re-downloadable) to the owner's Duplicati on Nemu, or a simple dated tarball with rotation. Owner to
   confirm the target.
   Status 2026-11-10: **DONE — tooling built AND running.** `scripts/backup_hub.sh` (eve) + `scripts/backup_pull.sh`
   (PC) + systemd user timers (`scripts/systemd/`) + docs/BACKUPS.md. Installed: `riko-backup.timer` on eve
   (03:20, linger on) and `riko-backup-pull.timer` on the PC (04:40), both enabled; first archives taken and
   verified (archive extracted, `PRAGMA integrity_check` = ok, 2,131 messages). Rotation keeps 14. Snapshots use
   the sqlite3 backup API inside the hub container (the image has no sqlite3 CLI). Remaining owner step: point
   Duplicati on Nemu at `~/Backups/riko-hub` on this PC.

## Owner's steps

8. Fill in each persona's **Appearance** on the hub web page.
9. Push / open a PR: everything is on the local branch `feat/local-desktop`, nothing is pushed yet.

## Loose ends

10. The status pill text during tool use is a stopgap: give the page a first-class `status(text=…)`.
11. The thinking-pose fingers only curl on rigs that have weighted finger bones (the sample avatar doesn't).
12. Test-run warnings (MCP SDK ping deprecation, FastAPI `on_event`); silence or migrate.
13. **DONE 2026-11-12 — Hops bend the legs** (owner request): docs/SELF_ANIMATION.md "Hops bend the legs"
    (planted crouch, airborne tuck, doc-time landing cushion; hop cookbook + lint on the hub).
14. **DONE 2026-11-12 (hub fix, needs hub redeploy)**: the greeting-audio cache key had no greeting text, so a
    clip cached while the greeting was empty (preview line) replayed after the greeting was set; PUT persona now
    drops cached persona audio when the greeting changes. Original report: **Greeting uses the persona's own line** (owner report 2026-11-12): on app start and on a
    persona switch, Jahoda (greeting "Jahoda, reporting for duty!") instead speaks "Senpai, this is what my voice
    sounds like" — the voice-sample/preview text, not her greeting. Find which path (startup greet vs the
    persona-switch/voice-preview path) feeds the TTS, make both speak the active persona's greeting, test the
    switch case too.
15. **Speech bubble above the head, not over the face** (owner request 2026-11-12, do after 13, done
    2026-11-13): the bubble now rides her live head — above the head when it fits (>= 2 readable lines,
    font may step down once), otherwise on the torso below the chin, `top` (slid beside her face in wide
    roam strips) only when neither fits; never over the face band, gated per frame by the `bubble`
    scenario in the capture harness (rules + measurements: docs/TOUCH.md "Speech bubble placement",
    evidence docs/evidence/bubble_placement_sample.png). Measured: the default 420x680 desktop window
    keeps only 28 px (sample rig) / 8 px (owner rig) of above-head space at visual scale 1.0 (head top
    y≈44/24, fit framing includes the wave envelope), so above-head fits only at hop apexes with the
    small font; a reserved top band in `applyFraming` (~+90 px) would make above-head fit 1-line
    replies — proposed, not implemented (framing changes out of scope here).
16. **Reduce memory use** (owner report 2026-11-12): loading the desktop assistant uses over 1 GB of RAM,
    which is too high. First measure: per-process RSS/PSS of desktop.py and its web-view (renderer, GPU,
    utility) with the owner's avatar vs the sample avatar, idle and after a few minutes of chat. Then cut
    the biggest costs, e.g. avatar textures (downscale/compress, free CPU copies after GPU upload), Chromium
    flags/process model, Python-side imports and caches. Set a target from the measurements and keep a
    repeatable measurement script.
