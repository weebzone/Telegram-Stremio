# Configuration

Configuration happens in **two layers**. Understanding this split makes everything
else easy.

```{list-table}
:header-rows: 1
:widths: 20 25 55

* - Layer
  - Where
  - What goes here
* - 🧱 **Startup**
  - `config.env` file
  - The core credentials needed to boot the app. Set once, before first launch.
* - 🎛️ **Runtime**
  - Web Settings page
  - Everything else — saved to the database and applied instantly, no restart.
```

```{admonition} Beginner Tip
:class: tip
Only 6 things go in `config.env`. If a setting isn't in the list below, you set it
later from the web panel — and changes take effect immediately.
```

## Part 1 — Startup variables (`config.env`)

These are read once when the app starts.

`API_ID`
: **Your Telegram API ID.** A number from [my.telegram.org](https://my.telegram.org).
  Needed so the app can talk to Telegram. **Required.**

`API_HASH`
: **Your Telegram API Hash.** A long string from the same page. **Required.**

`BOT_TOKEN`
: **Your bot's token** from @BotFather. This is the bot that receives files and hands
  out streaming links. **Required.**

`OWNER_ID`
: **Your numeric Telegram user ID.** Marks you as the owner/super-admin. **Required.**

`DATABASE`
: **Two MongoDB connection strings**, separated by a comma. The first is the
  *tracking* database (settings, tokens, catalogs); the second is *storage_1*
  (your media references). **Required.**

`PORT`
: **The web server port.** Defaults to `8000`. Your domain/reverse proxy points here. **Required.**

`USER_SESSION_STRING`
: **A Telegram "stay logged in" token for your own account.** Only needed to unlock
  **Global Search**. Safe to leave empty. **Optional.**

```{admonition} Why is USER_SESSION_STRING special?
:class: note
A bot can only see channels it's a member of. To search *other* channels you're in,
the app briefly uses your own account session. It's the only runtime feature that
needs a value in `config.env` plus one restart. See {doc}`search`.
```

## Part 2 — Runtime settings (Web Settings page)

Open **Settings** at `/admin/settings`. These are stored in the database and applied
live. Here's what each one does.

### General

**Replace Mode**
: When ON, uploading a file with the **same quality label** (e.g. another `720p`) as
  an existing one **replaces** the old entry instead of adding a duplicate. Recommended **ON**.

**Hide Catalog**
: Hides the public browsable catalog in Stremio. Direct streams still work — useful if
  you only want people to open specific links, not browse everything.

### Admin Authentication

**Admin Username / Password**
: Your web panel login. Defaults are `admin` / `admin`.
  **Change these immediately.** Leave the password blank when saving to keep the current one.

**AUTH_CHANNELS**
: The channel(s) the bot **indexes and streams from**. Add each by `@username` or its
  `-100…` ID. Your bot must be an **admin** in each channel.

```{admonition} Common Mistake
:class: warning
If a channel isn't listed in AUTH_CHANNELS, the bot **ignores** its files completely.
This is the #1 reason "nothing shows up."
```

### Media & Content

**TMDB API Key**
: A free **v3** key from [themoviedb.org](https://www.themoviedb.org) → Settings → API.
  Powers automatic metadata, posters, and language/OTT catalogs.

**Base URL**
: Your public address, e.g. `https://your-domain.com`. **Critical:** Stremio uses this
  exact address to reach your streams. If it's wrong, playback fails.

**Upstream Repo / Branch**
: Optional. Used by the update process to pull the latest code (e.g. repo
  `weebzone/Telegram-Stremio`, branch `master`). See {doc}`updating`.

### Subscription (optional)

Turn this on to charge for access. You configure:

- **Subscription Group ID** — the channel users must join.
- **Payment Instructions** — your UPI / bank / PayPal text.
- **Payment QR image URL** — optional QR code image.
- **Approver IDs** — Telegram user IDs allowed to approve/reject payments.

Full flow is in {doc}`user-management`.

### Global Search (optional)

Requires `USER_SESSION_STRING` in `config.env` plus one restart. Then enable the toggle
and add the **channel IDs** to search. See {doc}`search`.

### Proxy (optional)

**HTTP Proxy URL**
: An external proxy address (from a proxy provider or one you host). When set, the app
  routes stream links through it. Leave empty for direct streaming (usually fastest).

**Show Proxy and Non-Proxy Both**
: When ON, Stremio shows two links per file — one proxied, one direct — so viewers can
  pick whichever works better for them.

```{admonition} Good to Know
:class: note
The proxy is **not** created by this app and doesn't magically speed things up. It's an
optional workaround for IP blocks or geo-restrictions. It can even slow streaming if the
proxy is slow.
```

### Extra Storage Databases

Your first two databases (from `config.env`) are **locked** as *Tracking* and *Storage 1*.
Add more MongoDB URIs here to expand capacity. A 🟢 means connected.

```{admonition} Common Mistake
:class: warning
Only remove databases from the **end** of the list. Existing media references databases
**by position**, so removing one from the middle breaks those links.
```

### Multi-Token Clients

Add extra **bot tokens** for faster parallel streaming under heavy load. Create more bots
with @BotFather, add them as **admins** in all your AUTH channels, then paste their tokens.
Changes apply immediately.

```{admonition} Beginner Tip
:class: tip
Each extra bot token is like adding another checkout lane at a store — more people can be
served at once. You only need this if many people stream simultaneously.
```

## Storage databases explained

You might wonder why there are multiple databases:

- **`tracking`** — the "brain": settings, tokens, subscriptions, custom catalogs, requests.
- **`storage_1`, `storage_2`, …** — the "shelves": the actual movie/TV entries and their
  Telegram file references.

When one storage database fills up (free tiers have size limits), you add another. The app
spreads media across them and remembers which database each title lives in.

```{admonition} Why split storage at all?
:class: note
Free MongoDB tiers cap at ~512 MB. Splitting across multiple free databases lets you grow
a large library without paying — each new database is another free "shelf."
```
