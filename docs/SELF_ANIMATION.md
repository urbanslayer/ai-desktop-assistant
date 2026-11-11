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
  normalized humanoid bone** (three-vrm 3 normalized rig). Bone local **axis signs are rig-specific** — they are NOT
  identical between avatars (see the measured convention table below; the live model and the QA sample avatar are
  axis-mirrored on several bones).
- **Hold semantics (2026-11 revision — replaces "missing = rest"):** a bone / expression / `hips_y` value **missing
  from a keyframe interpolates** between the nearest keyframes where that track IS specified (linear in time), and
  the FIRST/LAST keyframes act as implicit rest/0 anchors when the track is absent there — animations still start and
  end at rest via the existing 0.25 s blend. You no longer need to re-key a bone on every key where it should hold a
  pose. `validate_animation` **expands every mentioned track into explicit per-key values at save time**
  (`fill_keyframe_holds`), so stored docs are unambiguous and even an old "missing = rest" player renders the same
  move (hub-normalise-at-save chosen over sampler parity because stored docs then need no semantics version flag at
  all); `client/js/keyframes.js` `holdFillKeyframes()` implements the identical fill defensively for cached /
  hand-written / fixture docs, and `client/js/fixtures/animation_hold_golden.json` is the parity fixture both sides
  test against (`hub/animations.py` fill output == fixture == page fill).
  Stored pre-revision rows are migrated **automatically on hub startup** (`migrate_hold_semantics`; idempotent;
  logs `hold-semantics keyframe migration rewrote N animation row(s)`).
- Allowed bones: `hips, spine, chest, upperChest, head, leftShoulder, rightShoulder, leftUpperArm, rightUpperArm,
  leftLowerArm, rightLowerArm, leftHand, rightHand, leftUpperLeg, rightUpperLeg, leftLowerLeg, rightLowerLeg,
  leftFoot, rightFoot` plus finger bones (`leftThumbProximal` … `rightLittleDistal`). **`neck` is not allowed**
  (Furina garment skinning, docs/BODY.md); `spine`/`chest`/`upperChest` are limited to ±15° per axis; `head` ±40°.
- **Bone conventions (⚠ measured 2026-11 — see "Measured pose cookbook" below):** VRM bone local axes are
  **rig-specific**, not standardised. The **live-model convention (Furina)**: `leftUpperArm`/`rightUpperArm`
  **Z raises the arm out to the side** (left −Z, right +Z; |z|≈70 ≈ out to the side, 100+ = overhead V),
  **Y swings it front/back** (−=forward), X ≈ twist (near no-op). The old doc said "left +Z raises the left
  arm" — that was measured on the **sample avatar**, whose arm/head/spine axes are **mirrored** vs Furina.
  Keyframe numbers are per-model assets (`animations.model_sha256`), so the cookbook is authored against the
  live rig and the table below records both rigs' signs.
- `expression`: VRM expression name (`happy, angry, sad, relaxed, surprised, aa, ih, ou, ee, oh, blink`) → 0..1.
- `hips_y`: optional vertical hip offset in metres, −0.25 .. 0.15 (a hop or a curtsy dip). Measured (2026-11):
  the player **translates the whole body** by the offset — with no leg bone keyed the feet ride with it and
  **leave the ground** (≈36 px of lift per 0.1 m at the QA capture zoom, airborne confirmed on both probe
  rigs), so a hop is visible without touching legs (this is the chosen hop behavior). There is no
  feet-pinned mode: keying legs only adds knee bends on top (keep `hips_y` near 0 for a planted crouch).
- Limits: the hub owns `hub/animation_limits.json` (per-bone min/max degrees per axis, matching the page's
  `pose.js` grab/joint limits) and **clamps** out-of-range values and **rejects** structural errors (bad name,
  reserved name, keyframe count/timing, unknown bone, `neck`) with a message the model can act on.
  The page clamps again on playback (defence in depth).

## Measured pose cookbook

> Renders of the owner's Furina model (third-party VRM, licence unknown) are kept out of the public repo; the sample-avatar renders in `docs/evidence/` are published.
 (2026-11, headless on both rigs)

Method: `client/tools/capture_frames.py custom … --gaze 0,0` on 3 s probe animations holding one bone at a
time, then **objective world-space deltas** from each frame's `bones`/`metrics` (hand/head/foot positions vs
the rest baseline; `eyesWorld` for head roll) plus vision confirmation on the contact sheets. Evidence PNGs:
`docs/evidence/pose_measured_*.png` (generated from `client/tools/probe_evidence/` probe runs; labels give
the exact keyed values). Rest pose = **arms at sides** (`{}`), not T-pose, on both rigs. Signs are for
`[X, Y, Z]` Euler degrees, local, from rest; "Furina" = the live model, "sample" = the QA sample avatar.

| Motion | Furina (LIVE) | sample avatar | Evidence |
| --- | --- | --- | --- |
| `upperArm` Z | **L −Z / R +Z raises the arm out to the side** (70 ≈ horizontal T, 100–110 overhead V); opposite sign crosses the arm over the body | **mirrored** (L +Z / R −Z raise) | Furina render (not published), `pose_measured_sample_arms.png` |
| `upperArm` Y | **− swings the arm FORWARD**, + swings back (both signs lift the hand slightly as the arm swings on its cone) | behaves differently (mixed swing+abduction); prefer Z moves on sample | world deltas (hands z ∓0.35 m at ±60°) |
| `upperArm` X | twist along the arm — near no-op on the hand (<0.01 m at 60°) | same (negligible) | both probe runs |
| `lowerArm` (elbow) | **hinge = Y: −75…−90 flexes the forearm FORWARD/up** (hand rises ~0.15–0.2 m, forward ~0.16 m); + swings it back; Z = small sideways swing (avoid); X = twist | **same sign** (Y− forward confirmed: hand z +0.22 at Y−60) | Furina render (not published) |
| `shoulder` | decorative: ±10–15 shifts the shoulder line slightly (L Z+ pulls the arm slightly inward) | same | probe run |
| `spine` / `upperChest` Z | **lean sideways: +Z leans to HER LEFT** (eyes shift +x); **sway = spine Z ±8…12 plus upperChest Z ±5…8 SAME sign**, alternate sign per key | Z lean works; direction mirrored (lean-right for +Z) | Furina render (not published) (spine rows) |
| `chest` | **no-op on the live rig — the VRM has no Chest bone; use spine + upperChest** | works | world probe (zero delta) |
| `spine` X | + leans BACK (eyes −z); − leans forward | **mirrored** (+ leans forward) | `pose_measured_sample_head.png` |
| `head` X | **+ = look UP (chin up), − = nod down** | **mirrored** (+ = nod) | Furina render (not published) vs `pose_measured_sample_head.png` |
| `head` Y | turn left/right (either sign turns) | either sign turns | same sheets |
| `head` Z | **tilt: Z+ tilts to HER RIGHT, Z− to her left** (ear toward shoulder — the "tilt to the music" axis) | mirrored (+ tilts her left) | Furina render (not published), `pose_measured_sample_roll.png` |
| `upperLeg` X | + swings knee/leg FORWARD (foot +z 0.35 m at +60°); − swings back | same (foot forward at +60°) | Furina render (not published) |
| `lowerLeg` X | **− is the natural knee bend (heel toward glutes)**; + raises the shin forward (kick) | **mirrored** (+ bends naturally) | same sheet |
| `hips_y` | translates the WHOLE body: +0.05…0.15 lifts the feet off the ground (≈36 px / 0.1 m airborne); − dips/sinks; keyed legs add bends but never re-pin the feet | same | Furina render (not published) |

Reference poses (live rig — exact numbers, each verified by capture; mirror L/R pairs as shown):

- **Arms at sides (rest):** `{}`. Not a T-pose.
- **Arms straight out (T):** `leftUpperArm [0,0,-70]`, `rightUpperArm [0,0,70]`.
- **V overhead:** `leftUpperArm [0,0,-100]`, `rightUpperArm [0,0,100]` (add `leftLowerArm/rightLowerArm
  [0,0,∓10]` to keep the arms straight-looking; clear the shoulder around t=0.6 with ±30 first for a smooth
  wind-up).
- **Hands on hips (waist-height guard):** `leftUpperArm [0,0,-20]` + `leftLowerArm [0,-80,20]`,
  `rightUpperArm [0,0,20]` + `rightLowerArm [0,-80,-20]` — hands land at waist height in front of the hips;
  on a straight-elbow rest rig they don't clasp the hip exactly (verified: hands Δy +0.15 m, Δz +0.16 m).
- **One arm waving overhead:** `rightUpperArm [0,0,95]` to raise (right = +Z on the live rig),
  `rightLowerArm [0,-60,0]` brings the forearm forward-up next to the head, then oscillate
  `rightLowerArm` Y between −40 and −90 across keys (or ±12° on upperArm Z for a wider wave).
- **Side-to-side sway:** spine `Z ±10` + upperChest `Z ±7` **same sign**, one sign per key, alternating;
  NOT the Y axis (Y is a waist twist).
- **Head tilt to the music:** `head Z ±12` matching (or countering) the sway sign — Z is the tilt axis,
  X is the nod, Y is the turn.
- **Small hop:** no leg keys needed: `hips_y` −0.03 (dip) → +0.12 (air — the whole body, feet included,
  rides the offset) → −0.02 (landing bend) → 0; add `head X -6` on the dip and arms swinging Y− on the
  rise for weight. For a planted crouch instead of a hop keep `hips_y` ≤ 0 and bend the legs.

### Proof: spotlight_twirl on the live model (Furina)

The owner's saved `spotlight_twirl` ("arms in a V, sway, head tilt, hop") rendered as "arms pointing out
from the elbows and nothing else" for three compounding reasons: the old missing=rest semantics snapped
lowerArm/shoulder/spine back to rest on every intermediate key; the sway was keyed on spine/head **Y**
(twist/turn) instead of **Z**; and the raise pair used the sample-avatar signs, which are **mirrored** on
Furina, so her "V" crossed the arms inward. Renders (headless Furina, ~8–12 timestamps each, ground line
drawn in red):

- `spotlight_as_stored` render (not published) — the stored doc rendered under old rest-fill semantics:
  arms inward/crossed mid-move, no sway, no tilt.
- `spotlight_renormalised` render (not published) — same key values after the hub's hold-fill expansion
  (`holdFillKeyframes`/`fill_keyframe_holds`): limbs hold between keys and the wind-up swing reads,
  but the axis/sign errors remain (still no V, sway is a twist).
- `spotlight_corrected` render (not published) — re-authored from the owner's description with the cookbook
  (V = L −100/R +100, sway = spine+upperChest Z ±10/7 alternating, tilt = head Z ±12, hop = hips_y
  −0.03→+0.14→−0.03 with a knee-steer): V overhead at t≈0.8–1.6, visible lean both ways, ear-to-shoulder
  tilt following the sway, and a hop whose airborne frame lifts the boot soles ~40 px off the ground line
  (raw lowest-ink row 620 vs planted 657–670), with crouch before and compression on landing.
- `spotlight_arm_lift_compare` render (not published) — as-stored vs renormalised at equal doc times (arm
  elevation difference from hold semantics alone).

All four stored live drafts (`cheeky_swayer`, `curtsey_twirl`, `hat_tip_hop`, `spotlight_twirl`) are
sparse and get rewritten to explicit per-key values by `migrate_hold_semantics` **automatically at hub
startup** (idempotent; logs the row count).

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

## Tool-made animations + the same-turn preview race (live bug fix, 2026-11)

Second live failure: she invented `curtsey_twirl` with `create_animation`, replied "shall I keep
it?", and the page logged `[director] unknown action 'curtsey_twirl' — ignored` anyway. Two causes:

1. **The tools never published `animations_changed`.** Only the HTTP animation routes did, so the
   desktop chain (`_on_animations_changed` -> `refresh_custom_animations` ->
   `/custom-animations.json` cache -> page reload) never learned about tool-created dances.
   `build_animation_specs` now takes the hub `events` bus and publishes
   `animations_changed {persona_id, model_sha256}` after every successful
   create/keep/delete/adapt mutation (failed mutations publish nothing).
2. **The reply races the refresh.** The preview play action ships in the same reply as the tool
   call — seconds before the async refresh can land. The preview action is now self-contained: the
   hub attaches the validated animation JSON inline (`{type:'gesture', custom:true, name,
   preview:true, animation:{…}}`, capped at 32 KB via `animation_tools.inline_animation_doc`;
   oversized docs ride the refresh path only). The page registers inline copies
   (`registerInlineAnimations` in `client/js/inline_anim.js`: `validateAnimation` ->
   `customAnims` -> `director.library`) BEFORE calling `director.say()`, so the name validates and
   the dance plays this same turn. A status `draft` is perfectly playable as a preview; only
   `kept` animations are offered for later `[act:]` tags (the contract is unchanged).
   Safety net: `Director` gained an `onUnknownAction` hook — when `say()` would still drop an
   unknown name the page runs one `loadCustomAnimations()` and plays the name if it appears
   within ~2 s (covers a kept animation referenced later while the cache lags).

Regression: `test_tool_mutations_publish_animations_changed` (+ bus-crash and size-cap cases), the
updated `test_chat_turn_create_animation_reply_carries_preview` (inline `animation` field +
`animations_changed` on `/api/events`), `client/js/inline_anim.test.js` (say with an inline custom
animation plays with zero unknown-action warnings; hook fires exactly when the name is unknown),
and the `custom_say_inline` capture scenario (`capture_frames.py custom_say_inline <out> <secs>`:
`/custom-animations.json` serves an EMPTY list — the animation exists only inline on the reply
action — and the `custom-gate` lines must still PASS mid-pose).

## The web Animations tab's Preview button (2026-11)

`POST /api/personas/{id}/animations/{name}/preview` publishes an `action` event
(`{persona_id, emotion:null, actions:[<preview action + inline animation>]}`) on the same event
channel chat replies ride. The desktop's events follower (`riko/session.py` `_on_hub_event`)
handles kind `action` for the ACTIVE persona only (dedupe on the event's `seq`+`time` against a
ring replay, same style as the timer handlers) and forwards `actions` to the avatar page as an
**actions-only page event** `{type:'actions', actions}` — the page handler registers the inline
copies BEFORE `director.say()` validates the names (identical ordering to a `say` carrying
actions) and schedules them with an empty line, so the dance plays immediately with NO bubble
and NO voice. The preview route answers `{ok, delivered}` honestly: `delivered:false,
reason:"no desktop online"` when no registered desktop client is online (still 200 — the event
is published for any other live-events follower), and the web page says
"Riko's desktop app isn't connected" instead of pretending it played.
Regression: the desktop tests (`test_action_preview_*` in `tests/test_custom_animations.py`),
the preview-delivery assertions in `test_animations_routes_crud_event_and_preview`, and the
`actions_preview_inline` capture scenario (`capture_frames.py actions_preview_inline <out> <secs>`:
actions-only event, EMPTY custom list, keyframe-gated shots must PASS with zero
"unknown action" console warnings).

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
