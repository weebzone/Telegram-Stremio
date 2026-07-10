# Troubleshooting

This is likely your most-visited page. Find your symptom and follow the fix.

```{admonition} First stop
:class: tip
Open the **Health Dashboard** in the admin panel. It quickly tells you whether Telegram,
your databases, and streaming are healthy — narrowing down the problem fast.
```

## Bot doesn't respond

- Confirm `BOT_TOKEN`, `API_ID`, and `API_HASH` are correct in `config.env`.
- Make sure the app is actually running (check logs / container status).
- Try messaging the bot again after a restart.

## Files aren't showing up / "nothing happens" when I upload

This is almost always one of these:

- The channel **isn't in AUTH_CHANNEL**. Add it in settings.
- The bot **isn't an admin** in that channel.
- The caption **lacks a resolution** (`720p`, `1080p`, …) — the file is skipped without one.
- The caption **lacks a title**.

```{admonition} Quick test
:class: note
Forward a file captioned exactly `Inception 2010 1080p BluRay.mkv`. If that shows up, your
earlier captions were the issue.
```

## No streams found (item shows but won't play)

- Check that **Base URL** matches exactly how you reach the server (including `https://`).
- Verify the Telegram file still exists (deleted messages are purged automatically).
- Look at the **Tools → Dead Links** report to find broken entries.

## Metadata missing or wrong

- Add a **TMDB API key** in settings — it dramatically improves matching.
- Fix the title/year in the caption, or use a **manual override** (IMDb/TMDB URL in the
  caption, or **Scan Metadata** in the panel). See {doc}`media-management`.

## Posters missing

- Same cause as missing metadata — the title didn't match a database entry, or there's no
  TMDB key. Re-scan the title with the correct match.

## Catalog is empty

- Automatic **language/OTT catalogs need a TMDB key** — without it only *Top Rated* and
  *Recently Added* fill up.
- Make sure you **enabled** and **saved** your auto-catalog selection.
- Run an **auto-sync** from the Catalogs page.

## MongoDB connection failed

- Re-check both URIs in `DATABASE` (username, password, and a database name at the end).
- In MongoDB Atlas → **Network Access**, allow `0.0.0.0/0`.
- Make sure the two URIs are separated by a **single comma with no spaces**.

## OffsetInvalid / FloodWait errors in logs

- **FloodWait** means Telegram is rate-limiting you; the app waits and retries. Slow down
  bulk scans or add more bot tokens to spread the load.
- **OffsetInvalid** usually clears on retry; if persistent, re-scan the affected channel.

## Stream stops midway / buffering

- Your server or proxy bandwidth may be the bottleneck — run a **Speed Test** in the panel.
- If you set an **HTTP Proxy URL**, try disabling it; a slow proxy hurts streaming.
- Add extra **bot tokens** so heavy concurrent use is spread across more bots.

## Subtitles missing

- Subtitle files must be uploaded to your channel; supported subtitle files are ingested
  automatically and attached to the matching title/episode.
- Make sure the subtitle's name matches the video closely enough to associate it.

## Playback has no sound

- Usually a **codec** limitation in your player, not the server. Try a different player
  (Nuvio is recommended) or a file with a more common audio track.

## Authentication failed (web panel)

- Default login is `admin` / `admin`. If you changed it and forgot, update
  `ADMIN_USERNAME` / `ADMIN_PASSWORD` and restart, or reset from your database.

## Something else?

Grab logs with the bot's `/log` command or **Settings → Logs → Download**, then open an
issue (see {doc}`support`).
