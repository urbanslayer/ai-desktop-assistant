# Touch and posing (BACKLOG item 7) — contract

Status: **approved by owner 2026-11-10 ("proceed with 7"), round T1 in progress.** Item 6 (she makes her own
animations) follows after T1 is merged and tested, and reuses the posing code built here (`client/js/pose.js`).

## What the owner gets

1. **Grab a limb.** Press and hold still (~250 ms) on her hand, forearm, foot or head, then drag: that limb follows
   the cursor through two-bone IK within joint limits. Let go and it eases back over ~0.4 s.
2. **Pat / poke / tickle.** Short clicks on her are recognised as touches:
   - `pat`: 2+ light taps, or a slow back-and-forth stroke, on the head;
   - `poke`: a single quick click on the body/arm (not the head);
   - `tickle`: rapid back-and-forth on the torso;
   - `grab` / `release`: a limb grab starting / ending (`hold_ms` on release).
   She reacts **instantly on the page** (expression + small move, no network), and the hub may add a short spoken
   line in character, rate-limited.
3. **Sit on the screen edge.** Drag her window down so her hips reach the bottom edge of the monitor: she blends
   into a sitting pose with her hips on the edge and her legs dangling past it (off-screen). Drag her back up
   (hips > 40 px above the edge) and she stands. Walking/roaming is paused while sitting.
4. **Click vs drag stays natural.** A press that moves > 6 px within 250 ms is still a **window drag** (today's
   behaviour), wherever you press. A press held still ≥ 250 ms **on a limb** grabs the limb. Held still on the
   torso/skirt → nothing special, then a later move drags the window. `Ctrl`+press forces a limb grab immediately;
   `Shift`-drag keeps its current meaning (move her inside the window). **No Super/Meta keys anywhere.**

## Seams (exact — both children implement these literally)

### Page → desktop (`riko/viewer.py` `POST /control/<cmd>`, JSON body)

- **`/control/bbox`** (existing) gains an optional field
  `parts: {head, hand_l, hand_r, arm_l, arm_r, foot_l, foot_r, torso, hips}` → each `[x, y, w, h]` in window CSS px
  (projected screen rect of that body part, padded ~6 px), plus `hips_y` (CSS px of the hips bone) and
  `feet_y` (lowest foot). Absent fields = unknown. Desktop must ignore unknown keys.
  Cadence: roam strip posts every 100 ms (walk housekeeping); NON-roam desktop mode samples at 10 Hz and
  posts only when the seam moved ≥ 2 px or once per second — the floating window needs the same
  `parts`/`hips_y` seam for limb-grab arbitration and the sit watcher, without walk state.
- **`/control/touch`** `{part, kind, x, y, hold_ms?, strokes?}` — `part` ∈ the names above; `kind` ∈
  `pat|poke|tickle|grab|release`. Posted once per recognised touch (page debounces: same part+kind ≤ 1 per 1.5 s,
  except `grab`/`release` which always post).
- **`/control/sit-state`** `{sitting: bool}` — the page's report after it finishes blending in/out.

### Desktop → page (bridge commands, existing `bridge.command.emit` → `on_command` in the page)

- `sit:<edge_y_css_px>` — the monitor's bottom edge is at this y in window CSS px (may be < window height when
  the window hangs off-screen). Page pins hips to that line and blends to the sit pose. Re-sent when it changes
  by ≥ 4 px.
- `stand` — leave the sit pose.
- `touch-mode:<off|quiet|chatty>` — page keeps instant local reactions unless `off` (then it still allows limb
  grabs but emits no touch reactions and posts no `/control/touch`).
- `press-hint:<limb|none>` — optional; not required.

### Desktop → hub

- **`POST /api/touch`** `{persona_id, part, kind, hold_ms?, sitting?}` →
  `{reacted: bool, actions: [...same shape as ChatResult.actions...], text: str|"" , audio_url: str|null,
    reason?: "cooldown"|"off"|"quiet"}`.
  - The hub ALWAYS answers fast (< 50 ms) when it decides not to speak (cooldown/off/quiet) — no LLM call.
  - When it speaks, it calls the LLM with the persona prompt plus one line of context, e.g.
    `(The owner just patted your head. React in one short in-character sentence; no tool calls.)` — max 1 sentence,
    `max_tokens` small, thinking allowed per current settings, emotion/action tags parsed by the existing
    `hub/body.py` path, TTS like any reply. The reply is stored in history as an assistant message with
    `meta.touch = {part, kind}` and the trigger as a **system note** (not a fake user message), so History shows
    "(patted her head)" in grey.
  - Rate limit per persona: settings `touch.mode` (`off|quiet|chatty`, default **quiet**),
    `touch.cooldown_s` (default 45 in quiet, 15 in chatty), `touch.max_per_10min` (default 4 quiet / 12 chatty).
    `quiet` speaks only for `pat`, `tickle`, and grabs held > 3 s; `chatty` for every kind. `grab` itself never
    speaks — only its `release`.
  - Never runs while she is already speaking/thinking for this persona (returns `reacted:false, reason:"busy"`).
- The desktop plays the returned reply exactly like a chat reply (bubble, TTS, actions) and never blocks the Qt
  thread on this call.

## Ownership (round T1)

| Path | Owner |
|---|---|
| `client/index.html` (touch/grab/sit wiring, bbox `parts`, page commands), `client/js/touch.js` (new: gesture recogniser + hit-testing), `client/js/pose.js` (new: limb-grab IK driver + sit pose + blend in/out; **item 6 will reuse it**), `client/js/*.test.js` for those, `client/tools/capture_frames.py` (new scenarios `grab`, `sit`, `pat`), `client/js/motion.js` / `director.js` only for hook points | **T1-A (page)** |
| `desktop.py` (press/hold/drag arbitration in `eventFilter`, sit-edge detection, touch forwarding, tray "Touch reactions: Off/Quiet/Chatty"), `riko/viewer.py` (new `/control/touch`, `/control/sit-state`), `riko/touch_desk.py` (new: pure arbitration + edge maths), `riko/session.py` (play a touch reply), `riko/client.py`/hub client wrapper if a method is needed, `hub/touch.py` (new), `hub/server.py` (route + settings + history note), `hub/static/index.html` (Settings ▸ Touch card; History grey note), `docs/API.md`, `character_config.yaml` `desktop.touch` block, tests for all of these | **T1-B (desktop + hub)** |
| `docs/TOUCH.md`, `docs/BACKLOG.md` | reviewer only — report contract concerns, don't edit |

## Rules for both children

- **Never** start a compositor/WM/X server (Hyprland, sway, kwin, weston, cage, Xvfb, Xephyr…), never query the
  owner's live session (`hyprctl`, `swaymsg`, `qdbus`, `busctl`, `grim`); unset `HYPRLAND_INSTANCE_SIGNATURE`,
  `WAYLAND_DISPLAY`, `DISPLAY`, `SWAYSOCK`, `DBUS_SESSION_BUS_ADDRESS` for every command. Window-manager behaviour is
  tested with recorded fixtures (`tests/fixtures/hypr_real_*.json`) and fake runners; the owner does the live
  check. Headless Chromium/offscreen Qt only.
- Garment skinning (docs/BODY.md): move limbs by their **own** bones; never rotate neck/chest to fake a reach.
  Gaze uses the head bone only.
- Joint limits for every IK result (reuse `client/js/ik.js`); grabbed hand may not pass through the torso
  (clamp the target outside a torso capsule).
- Sit pose: hips pinned to the edge line, thighs roughly horizontal toward the viewer (+z), shins hanging down,
  feet relaxed; spine slightly forward; hands resting beside the hips or on the lap. It must work for VRM0 and VRM1
  (Furina = riko persona model; Nicole; Asuka).
- No new runtime network fetches (the page is vendored/offline: `tests/test_client_vendor.py` must stay green).
- Settings with secrets/write-only rules unchanged; new settings are plain values.
- Commit after each green milestone.

## Speech bubble placement (BACKLOG item 15)

The speech bubble never sits on her face. While it is shown, the page re-places it EVERY frame
(cheap: only while shown) from the LIVE rig — `placeBubble` + `BubbleTracker` in `client/js/bubble.js`
(pure, `node --test` covered), driven by `bubblePlacement()` in `client/index.html`:

- **above** (preferred): the bubble over her head, bottom edge >= 8 px above the head top (hair/hat pad
  included: rest-pose `bboxTop - headTop` is added to the live skull top), horizontally centred on the
  projected head and clamped inside the window with 8 px margins. If the text would not leave >= ~2
  readable lines, the font steps down once (13 px) and the text scrolls inside `max-height` before the
  mode gives up. A CSS tail (`::after` triangle) points down at her.
- **body** (fallback): over the chest/torso, top edge below the chin + 8 px, so eyes/mouth are never
  covered; `max-height` runs down to the hips. Tail points up at her.
- **top** (last resort): the old pinned-to-top behaviour, only when the window is too small for both.
- Head/face/torso bands are projected from normalized bones (VRM 0.x and 1.0 alike; rigs missing bones
  fall back to `rig.headTop` / `rig.chest` / bbox). Mode switches carry 0.5 s hysteresis and the
  position is low-passed, so hops, sways and zoom never make the bubble flip or jump. Roam-strip mode
  uses the SAME placement (the old `--roam-x` bubble special case is gone; the status pill still rides
  `--roam-x`).
- Presses on the bubble still pass through to touch (unchanged rule above). The approval pill and the
  error bubble keep their own styles; the error bubble rides the same placement because it is `#bubble`.
- QA seam: `metrics.bubbleQA` in `window.rikoMeasure` (mode + bubble/head/face/body rects in CSS px),
  gated by `evaluate_bubble_gates()` in `client/tools/capture_frames.py` (scenario `bubble`): bubble
  rect never intersects the face band, `above` keeps >= 6 px over the head top, bubble fully inside
  the canvas. The `?capture` PNGs composite the real bubble DOM rect + the QA bands (the harness shoots
  the WebGL canvas, which misses DOM).

Measured headroom above the head at visual scale 1.0 (fit framing includes the wave/think envelope,
so the head sits near the window top; usable above-space = head-top y − 16 px gap+margin): default
420x680 gives 28 px (sample rig, head top y≈44) / 8 px (owner rig, y≈24); 1000x900 gives 52 px /
23 px. Two readable lines need ~61 px, so at idle `above` fits NONE of short/1-3-sentence/6+-sentence
replies at the default size, and only transiently (hop apex, small font) at 1000x900 — `body` is the
workhorse mode. A ~90 px reserved top band in `applyFraming` would make `above` fit one-line replies
at the default size (proposed in BACKLOG item 15, not implemented — framing changes are out of scope).

### Speech bubble headroom (owner decision 2026-11-13)

Owner decision: the bubble belongs ABOVE her head, so while she is talking the DESKTOP WINDOW GROWS
UPWARD and gives the room back afterwards — feet stay where they are, she does not change size, and
nothing is permanently re-framed. Reserved-band framing stayed out; the window moves instead.

- **Page** (`client/js/bubble_room.js`, pure + `node --test`, wired in `bubblePlacement()`): when the
  bubble is shown and `above` does not fit, the missing pixels are
  `bubbleH + ROOM_GAP(8) + ROOM_MARGIN(8) − headTopY`, rounded UP to `ROOM_STEP` (16 px) and capped at
  `ROOM_MAX` (220 px), and posted to the desktop. While the bubble is up the target never shrinks (a
  shorter line does not take room back), it only grows when the text needs more, and a keep-alive
  re-ask every `ROOM_ASK_MS` reconciles refusals/clamps. When the bubble has been gone for
  `ROOM_RELEASE_MS` (1.5 s — consecutive lines and replies reuse the room instead of pumping the
  window) exactly ONE release (`extra: 0`) is sent. Roam STRIP mode never asks: its band already
  slides beside her head.
- **Seam**: `POST /control/bubble-room` `{extra: <css px>}` → `{ok, granted, reason}` (`extra: 0` =
  give it back; `reason` is `grown|already|no-room|released|roam|hidden|gesture|no-desktop|error`).
  `granted` is what the window REALLY got — it can be less than asked (monitor top) or 0, and the
  page keeps `body` placement until it is enough. The desktop's `argv` evidence is stripped before
  the page sees the reply.
- **Desktop** (`riko/bubble_room.py` pure geometry + `HyprlandBubbleRoom`, route
  `handle_bubble_room_post` in `desktop.py`): grow = `y -= extra`, `h += extra`, `x`/`w` untouched,
  clamped so the top stops at the monitor's usable top; ONE batched `hyprctl --batch` (resize ; move,
  the same cached window/monitor state and dispatcher as the scroll-zoom sizer in
  `riko/avatar_scale.py`) per decision. Release restores the pre-grow rect exactly and the grown
  geometry is NEVER persisted as her saved size/position. Guards that defer without touching the
  window: roam strip active, window hidden, a scale gesture in flight (the page re-asks on its
  keep-alive). A user drag calls `mark_dirty()` at drag end, so the release restores around WHERE
  THE USER LEFT HER; a scale FIT that changes her normal size mid-bubble calls `rebase(w, h)` so the
  room is re-measured from the new size instead of resizing back to the old one.
- **Why she does not move**: the canvas resize re-frames through the existing zoom invariant
  (`syncViewport` → `applyScaleFrame`): pixels-per-metre preserved and her feet anchored a fixed
  margin above the canvas BOTTOM. The window top moved up by exactly the granted px, so on screen
  every pixel of hers stays put and all of the new space is above her head — where `placeBubble`
  then puts the bubble.
- **Click-through**: the new band is pass-through like the rest of the transparent window; the bubble
  itself is reported in `hit_extras` (with the ticker button) by `bboxBody`, and
  `riko/touch_desk.py:interactive_rects()` carries ALL those islands into the roam click-through
  mask and the press arbiter, so a press on the bubble above her head reaches the page instead of
  dragging the window.
- **Harness + gates**: `client/tools/capture_frames.py --room` (bubble scenario) answers the seam
  inside the page (`FAKE_DESK`): the fake desktop window is `--size` (passed to the page as
  `?fakebase=WxH`) and the BROWSER window is 240 px taller so the grown canvas fits — i.e. growing
  upward for real. Per-frame gates in `evaluate_bubble_gates()` (`metrics.roomQA` +
  `bubbleQA.room`, measured against the last pre-grow frame): granted room ⇒ `mode=above`; asked but
  not granted ⇒ NOT `above` (no claiming the head slot the desktop did not give); canvas width
  unchanged and height = pre + granted; her feet row and head row moved DOWN by exactly the granted
  px (so on screen: unchanged, same size, ±1 px); and across the run the window must come back to its
  pre-grow size after release. Evidence: `docs/evidence/bubble_placement_sample.png` (before = no
  room, bubble on her chest; after = grown window, bubble above her head).

## Live-issue notes (Nov 2026 grab investigation)

Owner report (Furina persona, normal desktop window, press held ~250 ms then dragged): "moving her hand
is quite tricky, and only the right hand seemed to move, I couldn't grab her left". Headless findings:

- **Proven + fixed — the speech bubble swallowed presses.** `#bubble` sits over the upper body during
  speech; while visible, any pointerdown landing on it never reached the canvas (HEAD code: 0/2 head
  grabs during a bubble, 1/1 after it hides; fixed code: 2/2 — bubble bottom measured at y=111 over a
  110 px press point, `client/tools/capture_frames.py custom_say` + `say:` touch-spec op). Presses on
  the bubble now pass through to touch (`#bubble` has no drag handler of its own, so nothing is lost).
- **Proven + fixed — far drags dead on the reach clamp.** The cursor-plane target was clamped to a
  sphere fixed to the rest anchor, so a 198 px drag moved the hand ~45 px and further pull bought
  nothing. The target is now the cursor ray ∩ reach sphere around the live shoulder
  (`clampGrabTarget` in `client/js/pose.js`, gated by `client/js/pose.test.js`): exactly under the
  cursor while reachable, always exactly reachable beyond it. Measured (sample rig, 420x680,
  `--desktop`): upward drags track the cursor within ~54 px (disp 153–160 px); downward/outward drags
  stay ~45 px because the arm is already fully extended toward the cursor — that residual is physical
  reach, not gating. Gates must state a minimum visible displacement, not only clamp correctness.
- **Proven + fixed — the capture harness re-delivered queued events.** `_due` pruned through an
  instance attr while `Handler.queue` is a class attr with a fresh handler per request, so every
  `/next` poll re-fired past events (82 duplicate `say`s kept the bubble up forever and skewed bubble
  A/Bs). `Handler.queue` is now pruned directly.
- **Unproven headlessly — a left-vs-right asymmetry.** With `character_files/sample_avatar.vrm` the
  posted part rects are symmetric (hand_l/hand_r 46x44 px, mirrored), page `hitTest` tries small parts
  first so the hand wins over `arm_l`, the desktop arbiter and hub reactions treat both sides
  identically, and synthetic grabs reach-and-hold on both hands. If the owner still cannot grab the
  left hand after the bubble fix, the remaining suspects are persona-rig-specific (Furina's rest pose
  or garment may shrink/overlap the left-hand rect in `computeParts`) or the 250 ms hold + 6 px slop
  window reading as a window drag. Next live step: `capture_frames.py grab` specs on the owner's
  Furina VRM (`--vrm`) comparing hand_l/hand_r rect sizes, or a `?debug` screenshot; if the rects
  differ, widen/precedence-fix `computeParts` for that rig rather than touching the arbiter.
