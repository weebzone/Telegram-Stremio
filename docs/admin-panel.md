# Admin Panel

The web admin panel is your control center — no command line needed for daily tasks. Log
in at `/login` (default `admin` / `admin`, which you should change immediately).

```{admonition} Beginner Tip
:class: tip
Almost everything you'd ever want to do lives here as a button or form. The old bot
commands for stats, logs, scanning, and restarting have all moved into this panel.
```

## Dashboard

Your landing page after login. Shows an at-a-glance overview of your library and system —
counts, recent activity, and quick links to the main sections.

## Media Library

Under **Media Management** you can browse **Movies** and **TV Shows**, search, and page
through everything. For each title you can:

- **Edit** its details or **Scan Metadata** to fix a wrong match.
- Delete a single **quality**, **episode**, **season**, or the whole title.
- **Manually add** custom movies, shows, seasons, episodes, or extra qualities.

See {doc}`media-management` for the details.

## Catalogs

Create and manage **custom catalogs**, set their **visibility**, and configure which
**automatic catalogs** are enabled. See {doc}`catalogs`.

## Users & Access

**Access Management** and **Subscription Management** let you handle tokens, plans, and
subscribers. See {doc}`user-management`.

## Content Requests

A public **request page** (`/request`) lets viewers ask for titles. Admins review incoming
requests under **Admin → Requests**, and can approve, update, or delete them. Fulfilled
requests can notify the requester automatically.

## Analytics

See live and recent **streaming stats** — active streams, throughput, and history — plus
tools to clear analytics. Helpful for spotting heavy usage or dead content.

## Health Dashboard

A **Health** report checks that your key pieces — Telegram connection, databases,
streaming — are responsive. Use it as your first stop when something feels off.

## Tools

The **Tools** page replaces the old scanning bot commands:

- **Scan** a channel to index existing files (start / cancel / view status).
- **DB Check** to verify database integrity (start / cancel / status).
- **Dead Links** detection and purge, to clean up entries whose Telegram files are gone.

```{admonition} Good to Know
:class: note
A **scan** is how you import files that were already in a channel *before* you set up the
bot. New uploads are indexed automatically without scanning.
```

## Settings

The full configuration page (`/admin/settings`) — general options, admin login, media
keys, subscriptions, proxy, extra databases, and multi-token clients. Everything here is
applied instantly. See {doc}`configuration`.

## Backup & Restore

From Settings you can **export** your configuration to a JSON file and **import** it later
— useful before migrating servers or making big changes. See {doc}`updating`.

## Bot commands

A few things still live in the Telegram bot itself:

```{list-table}
:header-rows: 1
:widths: 20 80

* - Command
  - What it does
* - `/start`
  - Returns your **addon URL** (or the subscription flow, if enabled)
* - `/set <imdb-url>`
  - Sets a metadata override for the files you upload next; send `/set` alone to clear it
* - `/log`
  - Sends the latest log file for debugging
* - `/restart`
  - Restarts the bot and pulls the latest updates from your upstream repo
```
