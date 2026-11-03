# Plan: making her move (animation)

Status: **plan approved, not started.** Owner decisions (2026-11-03): walking = overlay strip along the bottom of the screen with click-through; clips = free VRoid/pixiv packs only, bundled; emotion tags = yes.

## Where we are

`client/index.html` (three.js + @pixiv/three-vrm 3.4) already has some life in it: it blinks every 2–6 s, the
spine breathes, the head sways a little on a sine wave, and the mouth (`aa`) follows the loudness of her voice.
There are no arm movements, no gestures, no reaction to what is happening, and she never leaves the spot.
The page already gets status events from the app (`listening`, `hearing`, `thinking`, `speaking`, `idle`,
`persona`), so it knows *when* to act.

## Design: three layers mixed every frame

1. **Procedural layer (always on, code only, works with any VRM)**
   - Blinking (exists), with an occasional double blink.
   - Breathing (exists).
   - **Looking around:** small eye movements every 1–3 s (`vrm.lookAt` toward a target that moves now and then),
     and the head follows big eye movements slowly. About every 20–40 s when idle she glances off to the side,
     or up, then back.
   - **Looking at you:** while listening or speaking, the gaze locks on the camera. Inside her window she follows the
     mouse pointer. Global pointer position isn't available on Wayland, so outside the window there's nothing to follow.
   - **Lip sync:** today it's loudness → `aa`. Improve it to a cheap vowel guess (band energies → `aa/ih/ou/ee/oh`)
     so the mouth shape changes and doesn't just open and close.
   - **Expressions:** `happy/angry/sad/relaxed/surprised` blend shapes driven by an emotion tag on each reply
     (see "Emotion from the brain" below).

2. **Gesture clips (VRMA, triggered by events)**
   - Use `@pixiv/three-vrm-animation` (v3.5.5, same authors as three-vrm). It loads `.vrma` files, the
     standard VRM animation format, and retargets them to any humanoid VRM, so one clip works for every persona.
   - Clip library, each a short one-shot with a blend in and out:

     | trigger | clip |
     |---|---|
     | persona switch / greeting / app start / you come back after a while | `wave` |
     | `thinking` status | `think` (hand to chin, weight shift); loops until the reply arrives |
     | `listening` | `listen` (lean in a little, head tilt) |
     | while speaking (random, every few sentences) | `talk_gesture_1..3` (open hand, shrug, point) |
     | idle, every 1–3 min | `stretch`, `look_around`, `fidget`, `hair_touch` |
     | emotion tag | `nod`, `shake_head`, `laugh`, `pout` (optional) |

   - Sources (licences to check per pack before bundling): pixiv's free **VRoid Hub VRMA motion pack**
     (greeting, peace sign, spin, poses: free to use), **Mixamo** clips converted to VRMA (free with an
     Adobe account; the files are not redistributable, so they're downloaded per user and not committed), or
     motions made in Blender / VRoid / VRM Posing Desktop. Clips live in `client/animations/`, with an optional
     per-persona override set uploaded to the hub (stored like avatars, versioned).
   - A small **animation director** in the page: a state machine on top of the status events. It never
     interrupts a gesture with another, falls back to procedural motion if a clip is missing, and plays the gesture
     layer on the arms/upper body while the procedural layer keeps the eyes/head/breath alive.

3. **Moving around the screen (walking)** — the hard part, because of Wayland
   - Apps can't move their own window on Wayland. Only the compositor can, and every compositor does it
     differently, so "move the window" would only work with Hyprland-specific calls.
   - The WM-agnostic answer is **one bigger see-through window that she walks inside**:
     - an "overlay strip" along the bottom of the screen (full width, her height);
     - a walk cycle clip plus root motion moving her left/right inside the strip, turning to face the direction;
       now and then she wanders, stops, looks around, sits on the edge (optional clip);
     - **click-through except on her**: the window's input region is updated every few frames to her on-screen
       bounding box, using Qt `setMask` (sets the Wayland input region), so clicks on empty space reach the
       windows behind. If a compositor ignores the input region, there's a setting to fall back to the current
       small window.
     - Where the compositor supports **wlr-layer-shell** (Hyprland, Sway, KDE; not GNOME), the strip can be a
       layer-shell surface through LayerShellQt. It then sits above normal windows without any window rules. Where it
       doesn't, it stays a normal frameless always-on-top window.
   - Walking is **opt-in** (`desktop.roam: off | strip`), and never while she's talking or you're typing.

## Emotion from the brain (small hub change)

- The hub asks the LLM to start each reply with one tag from a fixed list (`[happy]`, `[teasing]`,
  `[annoyed]`, `[sad]`, `[surprised]`, `[thinking]`, `[neutral]`), strips it from the text, stores it on the
  message and returns `emotion` in ChatResult (and on the state poll for other clients).
- Persona setting: `emotes: true|false`, plus optional extra tags.
- Breeze voice direction can use the same tag ("say it teasingly"). That ties expression and voice together.

## Work split (when approved)

- **A1** animation director + procedural layer + VRMA loader + `think/wave/listen` with bundled free clips,
  plus page tests (`node --check`, a headless three.js smoke test with a sample VRM).
- **A2** hub emotion tags (prompt, parser, storage, API, web page toggle) + client expression mapping.
- **A3** roaming overlay strip (Qt input-region mask, optional LayerShellQt, walk clip, settings), needing a windowed
  check on the owner's desktop (screenshots only with permission).
- A1 and A2 can run in parallel. A3 comes after A1.

## Open questions for the owner

1. Walking: overlay strip along the bottom (recommended), stay inside the current small window (just pacing and
   turning), or skip walking for now?
2. Animation clips: free VRoid/pixiv packs only (bundled, licence-clean), or also Mixamo (better variety, each user
   downloads them with their own Adobe account)?
3. Emotion tags on replies: yes or no?
