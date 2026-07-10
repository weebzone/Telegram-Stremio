# Updating, Backup & Restore

Keeping your server current and your data safe.

## Updating the app

There are two common ways to get the latest version.

### Option A — `/restart` (built-in updater)

If you set **Upstream Repo** and **Upstream Branch** in settings, sending `/restart` to
your bot pulls the latest code from that repo/branch and restarts.

```{admonition} Beginner Tip
:class: tip
Set Upstream Repo to `weebzone/Telegram-Stremio` and branch to `master` (or your own fork)
so `/restart` can update you in one step.
```

### Option B — Redeploy

- **Docker Compose (VPS):**

  ```bash
  git pull
  docker compose up -d --build
  ```

- **Hugging Face:** push to your fork (or click **Run workflow**) — it auto-rebuilds.

## Backup your data

Two things are worth backing up:

**Your configuration**
: From **Settings**, use **Backup → Export** to download a JSON file of your settings.
  Restore later with **Import**.

**Your MongoDB databases**
: These hold your entire library and users. Back them up with your provider's tools or
  `mongodump`:

  ```bash
  mongodump --uri="<your-mongodb-uri>" --out=backup/
  ```

## Restore

- **Config:** Settings → **Import** the JSON file you exported.
- **Databases:** restore with `mongorestore`:

  ```bash
  mongorestore --uri="<your-mongodb-uri>" backup/
  ```

## Migrating to a new server

1. **Export** your config JSON and back up your databases.
2. Stand up the app on the new host (see {doc}`deployment`).
3. Point `DATABASE` at the same MongoDB (or restore your dump there).
4. **Import** your config, update **Base URL** to the new address, and save.

```{admonition} Common Mistake
:class: warning
After migrating, don't forget to update **Base URL** to the new domain/IP. Old stream
links point at the address stored in Base URL.
```

```{admonition} Order matters for storage databases
:class: warning
Media references storage databases **by position** in the list. When migrating, keep the
same database order so existing titles keep resolving to the right files.
```
