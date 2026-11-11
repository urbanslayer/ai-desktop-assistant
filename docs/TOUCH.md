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
