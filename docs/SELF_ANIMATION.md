# She makes her own animations (BACKLOG item 6) — contract

Status: **started 2026-11-11 (owner: "when 7 is completed and tested, implement 6 over night").** Round A6 builds
tier 1, **LLM-authored keyframes**, on the pose layer from item 7 (`client/js/pose.js`). Tier 2 (text-to-motion
models on eve: MoMask / T2M-GPT / MotionGPT / HY-Motion → VRMA) stays **plan only**.

## What the owner gets

- "Make up a little victory dance" / "invent a curtsy" → she calls `create_animation`. The hub validates and clamps
  her keyframes, saves them as a **draft**, and she **performs it right away as a preview**, then asks whether to
  keep it. "Keep it" → `keep_animation`. "No" / "delete it" → `delete_animation`.
- Kept animations play later like the built-ins: she can use `[act:victory_dance]` in replies, or the owner asks
  "do your curtsy".
- **Per avatar:** animations belong to a persona **and** the avatar model they were made on (sha256 of the persona's
  VRM file, computed by the hub). Switching model shows only that model's set. `adapt_animation(name, from_model)`
  copies one from another model of the same persona (humanoid normalized bones → same keyframes, re-clamped).
- Web page: **Animations** section per persona: list (name, description, duration, model, draft/kept, created),
  Preview (plays it on the desktop), Rename, Delete, Keep. Setting `animations.auto_keep` (default **false**).
- She can't overwrite built-ins: names of built-in gestures (`client/js/gestures.js`), VRMA clips and action
  tags are reserved.

## Keyframe format (the seam — both children use it literally)

```json
{
  "name": "victory_dance",            // ^[a-z][a-z0-9_]{1,31}$, not reserved
  "description": "both fists up, little hop",
  "duration_s": 2.4,                  // 0.4 .. 8.0
  "loop": false,
  "ease": "smooth",                   // smooth | linear
  "keyframes": [                      // 2 .. 24, t strictly increasing, first t=0, last t=duration_s
    { "t": 0.0, "bones": {}, "expression": {} },
    { "t": 0.6, "bones": { "leftUpperArm": [0, 0, 70], "rightUpperArm": [0, 0, -70],
                           "leftLowerArm": [0, -90, 0] },
      "expression": { "happy": 0.8 }, "hips_y": 0.04 },
    { "t": 2.4, "bones": {}, "expression": {} }
  ]
}
```

- `bones`: humanoid bone name → **Euler degrees `[x, y, z]`, XYZ order, local, relative to the rest pose of the
  normalized humanoid bone** (three-vrm 3 normalized rig, so the same numbers work on VRM0 and VRM1). A bone missing
  from a keyframe = rest (0,0,0) at that key.
- Allowed bones: `hips, spine, chest, upperChest, head, leftShoulder, rightShoulder, leftUpperArm, rightUpperArm,
  leftLowerArm, rightLowerArm, leftHand, rightHand, leftUpperLeg, rightUpperLeg, leftLowerLeg, rightLowerLeg,
  leftFoot, rightFoot` plus finger bones (`leftThumbProximal` … `rightLittleDistal`). **`neck` is not allowed**
  (Furina garment skinning, docs/BODY.md); `spine`/`chest`/`upperChest` are limited to ±15° per axis; `head` ±40°.
- `expression`: VRM expression name (`happy, angry, sad, relaxed, surprised, aa, ih, ou, ee, oh, blink`) → 0..1.
- `hips_y`: optional vertical hip offset in metres, −0.25 .. 0.15 (a hop or a curtsy dip). Legs: unless any
  leg bone is keyed, feet stay planted.
- Limits: the hub owns `hub/animation_limits.json` (per-bone min/max degrees per axis, matching the page's
  `pose.js` grab/joint limits) and **clamps** out-of-range values and **rejects** structural errors (bad name,
  reserved name, keyframe count/timing, unknown bone, `neck`) with a message the model can act on.
  The page clamps again on playback (defence in depth).

## Seams

### Hub (owned by A6-hub)

- `GET /api/personas/{id}/animations?model=current|all&status=kept|draft|all` → `{model_sha256, animations:[{id,
  name, description, duration_s, loop, ease, keyframes, status, model_sha256, created_at, updated_at}]}`.
  Default `model=current&status=all`.
- `POST /api/personas/{id}/animations` (create; body = keyframe object; returns the saved, clamped object plus
  `clamped: [..notes..]`), `PUT /api/personas/{id}/animations/{name}` (rename/description/keep:
  `{name?, description?, status?: "kept"}`), `DELETE /api/personas/{id}/animations/{name}`,
  `POST /api/personas/{id}/animations/{name}/preview` (publishes a play action to clients).
- Event on the existing hub event channel: `animations_changed {persona_id, model_sha256}`.
- Tools: `create_animation(animation)`, `keep_animation(name)`, `delete_animation(name)`, `list_animations()`,
  `adapt_animation(name, from_model?)`. create → saved as draft (or kept if `auto_keep`) and the reply carries the
  play action `{type:"gesture", name, custom:true, preview:true}`.
- Action tags: `hub/body.py` accepts `[act:<name>]` for this persona's **kept** animations on the current model and
  emits `{type:"gesture", name, custom:true}`; unknown names are still dropped.

### Desktop + page (owned by A6-page)

- The desktop fetches the active persona's animations for the current model (status kept + drafts) on startup,
  persona switch and `animations_changed`, and serves them to the page at **`GET /custom-animations.json`**
  (riko/viewer.py) as `{animations:[...]}`. The page reloads it on bridge command `custom-animations:reload`.
- Page: `client/js/keyframes.js` (new, pure): sample(anim, t) → per-bone quaternions (slerp between keys; `smooth` =
  cubic ease in/out per segment), expression weights, hips_y; blend in/out 0.25 s; clamps with pose limits. It is
  applied through the **pose layer** (`pose.js` — add an additive API such as `pose.playKeyframes(anim)` /
  `pose.stopKeyframes()`), after motion/director, before `vrm.update()`. While a custom animation plays, idle
  gestures and walking pause, gaze stays on (head bone only), a limb grab or sit **interrupts** it cleanly.
- `applyAction({type:"gesture", custom:true, name})` plays it from the loaded set; unknown name → no-op + console
  warning.

## Ownership (round A6)

| Path | Owner |
|---|---|
| `hub/animations.py` (new), `hub/animation_limits.json` (new), `hub/server.py` (routes, event, store schema bump), `hub/store.py`, `hub/body.py` (custom action tags), `hub/tools/*` (tools + prompt line), `hub/static/index.html` (Animations section), `docs/API.md`, `docs/TOOLS.md`, hub tests | **A6-hub** |
| `client/js/keyframes.js` (new) + test, `client/js/pose.js` (additive API only), `client/index.html`, `client/tools/capture_frames.py` (scenario `custom`), `riko/viewer.py` (`/custom-animations.json`), `desktop.py` + `riko/session.py`/hub client (fetch, cache, reload on event/persona switch), desktop tests | **A6-page** |
| `docs/SELF_ANIMATION.md`, `docs/BACKLOG.md` | reviewer only |

Shared test fixture: `tests/fixtures/animation_victory.json` — **A6-hub creates it** (a valid animation in the format
above); A6-page creates its own copy at `client/js/fixtures/animation_victory.json` (no shared file).

## Reply-action wiring (live bug fix, 2026-11)

A keyframe animation is only playable from a hub reply when the director knows its name:
`director.say()` validates reply actions against `director.gestures` BEFORE the page's `applyAction`
custom branch can play it. `setCustomAnimations` therefore registers every validated animation with
`director.library({name: {loop, duration_s}})` (same seam the `.vrma` drop-ins use), and the `gesture`
branch of `applyAction` additionally checks `customAnims` (director replays actions on emotion change
without the `custom` flag); `gesture_stop` ends a looping custom animation. Regression: the
`cheeky_swayer` case in `client/js/director.test.js` (unknown-action warning must stay silent) and the
`custom_say` capture scenario (`capture_frames.py custom_say <out> <secs>`: a queued `say` event whose
`actions[0].name` names the served animation; the `custom-gate` lines must PASS and the event-driven
kfwatch frames must show a real mid-pose).

## Rules for both children

- Never start a compositor/WM/X server; never query the owner's live session (`hyprctl`, `swaymsg`, `qdbus`,
  `busctl`, `grim`); unset `HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY DISPLAY SWAYSOCK DBUS_SESSION_BUS_ADDRESS`;
  offscreen Qt / headless Chromium only; no windowed launches or desktop screenshots.
- **Disk:** `/home` btrfs metadata is ~91% full with no unallocated space. Do not delete anything outside your own
  temp/capture dirs (never clear `~/.cache`), reuse one capture dir, run the full suite once at the end; on ENOSPC
  retry once, then report BLOCKED-DISK.
- Garment skinning (docs/BODY.md): never rotate `neck`; spine/chest only within ±15°.
- The page stays offline (`tests/test_client_vendor.py` green).
- Commit after each green milestone; do not merge; never run `git stash` in the main checkout
  (`~/Development/riko_project` itself) — work only inside your own worktree.
