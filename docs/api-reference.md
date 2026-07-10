# API Reference

This page documents the HTTP endpoints your server exposes. Most users never touch these
directly — Stremio calls them for you — but they're useful for debugging and integrations.

```{admonition} Beginner Tip
:class: tip
You don't need to understand this page to use the app. It's here for the curious and for
developers building on top of it.
```

## Authentication

There are two kinds of protected routes:

**Addon token** (`{token}` in the URL)
: Every Stremio-facing route is scoped to a per-user token, e.g.
  `/stremio/{token}/manifest.json`. Invalid or expired tokens are rejected (or shown a
  subscription prompt).

**Admin session** (web panel)
: All `/api/admin/**` and management routes require you to be logged into the web panel.
  Unauthenticated requests are redirected to `/login`.

## Stremio addon endpoints

These follow the standard Stremio addon protocol.

```{list-table}
:header-rows: 1
:widths: 55 45

* - Endpoint
  - Purpose
* - `GET /stremio/{token}/manifest.json`
  - The addon manifest — declares catalogs and capabilities for this user
* - `GET /stremio/{token}/catalog/{media_type}/{id}.json`
  - A catalog listing (latest / popular / custom)
* - `GET /stremio/{token}/catalog/{media_type}/{id}/{extra}.json`
  - Same, with extra filters (genre, search, skip/pagination)
* - `GET /stremio/{token}/meta/{media_type}/{id}.json`
  - Detailed metadata for one title, including the episode list for series
* - `GET /stremio/{token}/subtitles/{media_type}/{id}.json`
  - Subtitles for a title/episode, sourced from subtitle files in your channels
```

### The manifest

The manifest is **dynamic per user**. Its name and description reflect subscription state
(e.g. `Telegram — Expires 28 Mar 2026`), and its version encodes the expiry so Stremio
detects updates when you change a subscription.

## Streaming endpoints

```{list-table}
:header-rows: 1
:widths: 55 45

* - Endpoint
  - Purpose
* - `GET` / `HEAD` `/dl/{token}/{id}/{name}`
  - The actual video stream (supports range requests for seeking)
* - `GET /thumb/{id}`
  - A public thumbnail image for a Telegram file (used as artwork)
* - `GET /sub/{token}/{id}/{name}`
  - Serves a subtitle file (small, served fully in memory)
* - `GET /stream/stats`
  - Live and recent stream telemetry
* - `GET /stream/stats/{stream_id}`
  - Detailed telemetry for a single stream
```

```{admonition} How proxying affects the stream URL
:class: note
When an **HTTP Proxy URL** is configured, the app builds the stream link by prepending the
proxy address in front of the `/dl/...` URL. With "Show Both" enabled, both the proxied and
direct links are offered to the player.
```

## Admin & management API (session-protected)

The web panel is powered by a large set of `/api/...` endpoints. Highlights:

```{list-table}
:header-rows: 1
:widths: 50 50

* - Area
  - Example endpoints
* - Media
  - `GET /api/media/list`, `PUT /api/media/update`, `DELETE /api/media/delete`
* - Rescan / metadata
  - `GET /api/media/rescan/search`, `POST /api/media/rescan/apply`
* - Manual add
  - `POST /api/media/resolve-telegram`, `POST /api/media/manual-add`
* - Custom catalogs
  - `GET/POST /api/custom-catalogs`, `.../items`, `.../auto-sync`, `.../auto-sync/settings`
* - Tokens & access
  - `POST /api/tokens`, `DELETE /api/tokens/{token}`, `/api/admin/access/**`
* - Subscriptions
  - `/api/admin/subscriptions/plans`, `/api/admin/subscriptions/users`
* - Requests
  - `GET /api/admin/requests`, public `POST /api/request/submit`
* - System & tools
  - `/api/admin/health`, `/api/admin/tools/scan/**`, `/api/admin/tools/dbcheck/**`, `/api/admin/backup/export`
```

## Response format & errors

- Successful API calls return **JSON**.
- Stremio endpoints return the shapes Stremio expects (manifest, catalog metas, streams).
- Common error behaviors:
  - **401 / redirect to `/login`** — you're not authenticated to the admin panel.
  - **Token rejected** — an invalid or expired addon token.
  - **Empty catalog/streams** — usually means nothing matched or the item has no files yet.

```{admonition} Good to Know
:class: note
CORS is open (`*`) so Stremio clients on any origin can talk to your addon.
```
