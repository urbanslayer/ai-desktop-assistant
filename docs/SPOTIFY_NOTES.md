# Spotify: verified constraints (checked 2026-11-10 against developer.spotify.com)

Owner decisions (2026-11-10): has **Premium**; will create the Developer Dashboard app when given
steps; desktop playback via **spotifyd managed by Riko** (device name "Riko"); **Spotify first**,
Tidal later once developer access is approved.

## Rules that change the design (verified 2026-11-10)

- **Redirect URIs** (`/documentation/web-api/concepts/redirect_uri`): HTTPS required, **except**
  loopback IP literals (`http://127.0.0.1:PORT/...`, `http://[::1]:PORT/...`). **`localhost` is not
  allowed.** A loopback URI may be registered **without a port** and the port added in the
  authorization request.
  → The OAuth callback CANNOT be the hub's `http://192.0.2.51:8002/...` (plain HTTP, not loopback).
  **Design:** the **desktop app** runs the browser step and receives
  `http://127.0.0.1:8911/callback` (fixed port, configurable), then hands the code to the hub, which
  exchanges and stores the refresh token (write-only). The web page can also do it purely client-side
  with PKCE against its own `http://127.0.0.1:PORT` listener only if a local helper exists; simplest
  is: connect from the desktop app, and every other client then uses the hub's stored token.
  For a headless hub-only setup, the page shows the URL and the owner pastes back the redirected
  `code=` value (paste-back fallback).
- **Development mode** (`/concepts/quota-modes`, Feb-2026 migration guide): app **owner must hold
  Premium** (app stops if it lapses); **5 users** per app max (fine: single user, no allowlist needed
  for yourself); Client IDs per developer is 25 as of July 2026; exceeding quota returns HTTP 429
  `QUOTA_EXCEEDED` (must back off); extended quota is companies-only (250k MAU), so we stay in
  development mode forever.
- **Endpoints removed for development-mode apps** created after 2026-02-11 (existing apps were
  migrated 2026-03-09 — a brand-new app gets the new rules):
  - `GET /search`: **`limit` max is 10** (default 5) — paginate with `offset` for more.
  - Batch fetches gone: `GET /tracks?ids=`, `/albums`, `/artists`, `/episodes`, `/shows`,
    `/audiobooks`, `/chapters` → fetch one by one.
  - Library: `PUT/DELETE /me/tracks|albums|...` and `/me/following` → generic
    `PUT/DELETE /me/library` (takes URIs); `GET /me/library/contains`.
  - Playlists: `/tracks` paths renamed to `/items`; playlist **items are only returned for playlists
    the user owns/collaborates on**.
  - Gone: `/browse/new-releases`, `/browse/categories*`, `/artists/{id}/top-tracks`, `/markets`,
    `GET /users/{id}`, `/users/{id}/playlists`.
  - Fields gone from responses: `popularity` (track/album/artist), `followers` (artist/user),
    `country`/`email`/`explicit_content`/`product` on `GET /me`, album `label`, `available_markets`
    (mostly), track `linked_from`. **`product` is gone, so Premium cannot be confirmed via the API**
    — treat a 403/404 on player calls as "is Premium still active?".
  - The **full Player API is still available** (play/pause/next/prev/seek/volume/shuffle/repeat,
    devices, transfer, currently-playing, recently-played, queue) — everything this feature needs.

## Consequences for the plan

- No batch-fetch shortcut when building now-playing/queue UI: fetch tracks individually (or rely on
  spotifyd's MPRIS locally, which we already prefer on desktop).
- Search results: max 10 per call; pick the top hit rather than listing many.
- "Add to library" uses `PUT /me/library` with a Spotify URI.
- Keep a strict quota discipline: cache device lists, prefer MPRIS for now-playing, no aggressive
  polling of `currently-playing`.
