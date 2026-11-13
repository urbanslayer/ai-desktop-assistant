# Round "walk + memory" contract

Status: approved by the owner (2026-11-04). Children follow this literally and report problems instead of
editing it. Two children run in parallel with disjoint files (ownership at the end).

## W. Walking along the bottom of the screen (client)

Builds on docs/ANIMATION.md §3 (owner decision: overlay strip, click-through except on her).

**Setting.** `desktop.roam: off | strip`, default `off` until the owner has tried it, then they decide. With
`strip`:

- **The window.** Her window becomes a see-through strip along the bottom edge of the screen her window is
  on: full screen width, height = `desktop.height` (680 by default), always on top, no decorations.
  She walks left and right inside it.
- **Click-through.** Only her on-screen shape (the projected bounding box of her body, plus a small margin)
  takes mouse input. Everywhere else, clicks reach the windows behind. Update the input region whenever she
  moves (throttled to about 10 Hz), and while she's being dragged.
- **Placement and click-through are compositor-dependent, so research and pick a working method in this
  order, and report which works where:**
  1. **wlr-layer-shell** (Hyprland, Sway, KDE Plasma, niri…; not GNOME) through LayerShellQt, if it can be
     driven from PySide6 (Python bindings, or its Qt shell-integration plugin plus window properties): anchor
     bottom+left+right, layer `top`, exclusive zone 0, keyboard interactivity on-demand.
  2. **XWayland fallback** (`QT_QPA_PLATFORM=xcb` for this window/process): works on almost every
     Wayland compositor and on X11. Position the strip with normal X11 geometry and do click-through with the
     X Shape input region (Qt `setMask`, or the window's X11 input shape).
  3. If neither works, fall back to today's small window (`roam` is then effectively off) with a clear
     terminal message.
  - Keep the existing small-window mode and its Hyprland niceties unchanged when `roam: off`.
  - **Never touch the owner's compositor config**, and no compositor-specific code paths beyond what already
    exists for the small window.
- **Movement.**
  - A walk cycle: procedural legs/arms/hips swing in `client/js/gestures.js` style, proportion-aware (step
    length from leg length), with root motion moving her along the strip at about 0.6 m/s scaled to her height.
  - She turns to face the direction she walks, then turns back toward the viewer when she stops.
  - She wanders now and then: when idle, every 1–4 min, a 1–6 s walk to a random spot. She never walks while
    listening, thinking, speaking or typing, or while the approval bubble is up. A walk in progress stops
    smoothly when one of those starts.
  - Speech bubble and status pill follow her (the bubble via the normal placeBubble placement,
    docs/TOUCH.md "Speech bubble placement"; the pill via `--roam-x`).
  - Dragging her (left-drag) moves her along the strip; right-drag / shift-drag / scroll keep their meanings.
  - Persistence: her last x position is saved and restored on start.
- **Director.** Walking is a director-level activity: a pure state, tested with `node --test`. Gestures
  don't start while she walks (a gesture request waits or stops the walk, except forced ones like W, which
  stop the walk first). The camera follows her across the strip; the canvas spans the strip.
- **Settings.** `desktop.roam`, `desktop.roam_speed` (multiplier, 1.0), `desktop.roam_every` (`[60, 240]`
  seconds), `desktop.roam_margin` (px kept free at each screen edge, 40).
- **Verification.**
  - Logic: node tests for the walk director and the walk cycle bounds (no limb out of range over a 10-minute
    sim), plus headless-page renders of the walk cycle (frames at several phases), via
    `client/tools/capture_frames.py` with a wide `--size`.
  - Windowed: you may not open windows on the owner's desktop. If a **nested headless compositor** is
    installed or easily available (e.g. `sway --headless` / `cage` / `weston --backend=headless` with
    `WAYLAND_DISPLAY` pointing to it, and `grim` inside it), you may run desktop.py there to prove the strip,
    the placement and the click-through. Otherwise say exactly what's unverified. Never use the owner's
    Wayland display.

## M. Long-term memory (hub)

- **Store.** A SQLite table `facts`, with an FTS5 index on `text`:
  - `id`, `text` (≤ 500 chars)
  - `scope`: `user` (facts about the owner, shared by every persona) or `persona:<id>` (that persona's own
    notes)
  - `pinned` (bool), `source_message_id` (nullable), `created_at`, `updated_at`, `last_used_at`, `use_count`
  - Migration via user_version.
- **Tools (hub tools, in the tool loop).** Permission `always` by default, editable like the other tools:
  - `remember(text, scope="user"|"persona", pinned=false)`: deduplicates near-identical text (FTS match plus a
    normalised-text compare) and updates instead of duplicating. Returns the id.
  - `recall(query, limit=5)`: FTS search over `user` facts plus the active persona's notes.
  - `forget_fact(id | text)`: deletes by id, or by best match when it's clear.
- **Prompt.** Every turn gets a "What you remember" block (about 600 tokens maximum):
  - all **pinned** facts first;
  - then the top FTS matches for the user's message;
  - then the most recently used facts, until the budget is full.
  - Instruction: use these naturally. When the user shares something lasting about themselves (name,
    preferences, projects, people, dates), call `remember`. Don't store secrets, passwords or one-off
    trivia. When asked to forget something, call `forget_fact`.
  - Facts that end up in the prompt get `last_used_at` / `use_count` bumped.
- **Settings.** `memory: {enabled: true, prompt_budget_tokens: 600, auto_remember: true}`, using the existing
  blank-means-default semantics. When `enabled` is false, there's no block and the tools aren't offered.
- **API.**
  - `GET /api/memory?scope=&q=&limit=&offset=` → list/search.
  - `POST /api/memory {text, scope, pinned}`.
  - `PUT /api/memory/{id}`, `DELETE /api/memory/{id}`.
  - `DELETE /api/memory?scope=user|persona:<id>|all` to forget everything in a scope; it needs
    `?confirm=yes`.
  - `GET /api/memory/export` (JSON) and `POST /api/memory/import`.
  - Events: `memory_changed {id, op}` on the events long-poll.
  - Deleting a persona deletes its `persona:<id>` notes.
- **Web page.** A **Memory** section:
  - a searchable list, the scope filter (About you / each persona), and a pin toggle;
  - inline edit, delete, add;
  - "forget everything" (typed confirmation), export/import;
  - and in Chat/History, a small "🧠 remembered: …" chip on replies whose turn called `remember`.
- **Verification.** Tests with a fake LLM that calls `remember`/`recall`/`forget_fact`: dedup, the scope rules,
  the prompt block budget and ordering, permissions, migration, page routes test. A real check against the
  real LLM:
  1. "my cat is called Miso and I'm working on a game called Starburst 1200";
  2. a fresh chat after clearing history, then "what's my cat's name?";
  3. switch persona and ask again (it's a user fact, so it's shared);
  4. "forget my cat's name".

## Ownership (this round)

| path | owner |
|---|---|
| `client/**`, `riko/**`, `desktop.py`, `main.py`, `bin/**`, `character_config.yaml`, `desktop/**`, README (walking + shortcuts sections), non-hub tests | **W** |
| `hub/**`, `tests/test_hub_*.py`, `docs/API.md`, README (memory section only: one short paragraph under the hub/web page section) | **M** |
| `docs/*.md` other than API.md | reviewer |

README: W and M must edit different sections only. M adds one paragraph titled "Memory", W edits the
walking/shortcuts parts. If both touch the same lines, the reviewer resolves it at merge.
