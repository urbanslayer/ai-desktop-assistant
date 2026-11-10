# Vendored libraries

The avatar page (`client/index.html`) runs fully offline: everything it loads at
runtime is served from this directory via the import map (BACKLOG quick win 6).
No library is fetched from a CDN any more.

Files were downloaded from the pinned jsdelivr URLs below on 2026-11-10 and are
checked in verbatim. `tests/test_client_vendor.py` asserts the import map points
here, the module dependency closure stays local, and every file's SHA-256 still
matches this table. To bump a version: re-download from the same URL pattern,
update the table (and the import map if a file moved), then run
`uv run pytest -q tests/test_client_vendor.py` and the headless capture.

## Contents

Paths are relative to `client/vendor/`. The import map maps `three/addons/` to
`./vendor/three/addons/`, so the addon files keep the relative imports three.js
uses upstream (e.g. `GLTFLoader.js` → `../utils/BufferGeometryUtils.js`); only
the addons actually imported by the page are vendored, plus their transitive
files (`three.module.js` → `three.core.js`).

| Library | Version | File | Source URL | SHA-256 | Licence |
|---|---|---|---|---|---|
| three | 0.179.1 | `three/build/three.module.js` | <https://cdn.jsdelivr.net/npm/three@0.179.1/build/three.module.js> | `ce6be0cb5ead0027e1f6094dd82ad43bba0886c03324a3b21fd9a33ac93fc2b4` | MIT (`three/LICENSE`) |
| three | 0.179.1 | `three/build/three.core.js` | <https://cdn.jsdelivr.net/npm/three@0.179.1/build/three.core.js> | `3cd45beb4da9ce682bb7f5894dafd8a146379aa4b6b5aa3acd9ef5d47fdaaf92` | MIT (`three/LICENSE`) |
| three | 0.179.1 | `three/addons/loaders/GLTFLoader.js` | <https://cdn.jsdelivr.net/npm/three@0.179.1/examples/jsm/loaders/GLTFLoader.js> | `caba6c51cfd8c7d5313bd7705a54b76bc0a7199d9822ecc497c5311eaffe8e5e` | MIT (`three/LICENSE`) |
| three | 0.179.1 | `three/addons/controls/OrbitControls.js` | <https://cdn.jsdelivr.net/npm/three@0.179.1/examples/jsm/controls/OrbitControls.js> | `b97879c748170baadeb3fb84cea1ffdf4674e283dc06042f34e2acb95a76042c` | MIT (`three/LICENSE`) |
| three | 0.179.1 | `three/addons/utils/BufferGeometryUtils.js` | <https://cdn.jsdelivr.net/npm/three@0.179.1/examples/jsm/utils/BufferGeometryUtils.js> | `cbcfe1864abedcc0122cb893373918fe14469491717a34ca6efcc38551805765` | MIT (`three/LICENSE`) |
| @pixiv/three-vrm | 3.4.1 | `three-vrm/three-vrm.module.min.js` | <https://cdn.jsdelivr.net/npm/@pixiv/three-vrm@3.4.1/lib/three-vrm.module.min.js> | `86c7eba82db64a26edaafb6eac45fa532d08fb449194b83f87343a95f488793d` | MIT (`three-vrm/LICENSE`) |
| @pixiv/three-vrm-animation | 3.5.5 | `three-vrm-animation/three-vrm-animation.module.js` | <https://cdn.jsdelivr.net/npm/@pixiv/three-vrm-animation@3.5.5/lib/three-vrm-animation.module.js> | `8b3985e9420efb549a1dc8f72bc88fd3b257979f769e0b23ff9f723b61ca3ecf` | MIT (`three-vrm-animation/LICENSE`) |

Notes:

- The three-vrm-animation bundle is self-contained: it imports only from `three`
  and bundles `@pixiv/three-vrm` internals itself (upstream build). The separately
  vendored `three-vrm` 3.4.1 is what the page's own imports use.
- Licence texts: `three/LICENSE` is the three.js MIT licence (Copyright c 2010-2025
  Three.js Authors); the two `LICENSE` files under `three-vrm*/` are the pixiv MIT
  licence (Copyright c 2019-2025 pixiv Inc.), taken from the npm tarballs at the
  pinned versions.
- What still needs a network on purpose: weather, web search and the LLM (internet,
  via the hub), Spotify, and reaching the hub / TTS / ASR servers on the LAN (eve).
  The page itself needs nothing else: it uses system fonts (no webfonts), and the
  VRM models are local files (character_files/, or the hub-cached persona avatars).
