# Backlog

Agreed with the owner on 2026-11-04. The order is the suggested order; the owner decides what goes next.

## Next up (suggested pairs, disjoint files)

1. **Walking along the bottom of the screen**: the overlay strip from docs/ANIMATION.md §3. Full width,
   click-through except on her (Qt input-region mask), LayerShellQt where the compositor supports it,
   a walk cycle plus root motion, wandering now and then, never while talking or typing.
   Opt-in `desktop.roam: off | strip`. Client side.
2. **Long-term memory**: hub tools `remember(fact, about?)` / `recall(query)` / `forget_fact(id)`.
   Facts about the owner are shared across personas, with optional per-persona notes. A web page section
   to view and edit them. Relevant facts go into the prompt. Hub side.

## Quick wins

3. **Works offline**: bundle three.js, three-vrm and three-vrm-animation under `client/vendor/`
   (pinned versions, licence files) instead of loading them from the jsdelivr CDN.
4. **Hub backups**: nightly copy of `~/AI/Docker/riko-hub/data` (and `breeze-tts/data/voices`, the ASR
   model is re-downloadable) to the owner's Duplicati on Nemu, or a simple dated tarball with rotation. Owner to
   confirm the target.

## Planned, waiting for the owner's go

5. **Calling other models** (OpenAI, Claude, Gemini, OpenRouter): docs/TOOLS.md §C. **Plan only; do not
   implement until the owner says go.**
6. **Home Assistant voice**: a `wyoming_openai` container on eve in front of riko-asr (:8001) for STT
   and Breeze for TTS, Wyoming on :10300, added to HA via the Wyoming integration.

## Owner's steps

7. Sign the Riko Helper extension (`web-ext sign --channel unlisted` with the AMO API key), install the
   `.xpi`, and pair it with `riko-ctl browser-token`.
8. Fill in each persona's **Appearance** on the hub web page.
9. Push / open a PR: everything is on the local branch `feat/local-desktop`, nothing is pushed yet.

## Loose ends

10. The status pill text during tool use is a stopgap: give the page a first-class `status(text=…)`.
11. The thinking-pose fingers only curl on rigs that have weighted finger bones (the sample avatar doesn't).
12. About 250 harmless warnings in the test run (MCP SDK deprecation of ping usage); silence or migrate.
13. Android app against docs/API.md (events long-poll, client-tool channel, body capabilities).
