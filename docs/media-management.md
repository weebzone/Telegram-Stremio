# Media Management

This is the most important page for day-to-day use. It explains **how the app reads
your filenames**, how it tells movies from TV shows, how split and combined files are
handled, how to add things by hand, and how to fix wrong metadata.

## How the app reads a filename

Every time you upload a file, the app runs its **caption or filename** through a
cleaning-and-parsing pipeline. Understanding this helps you name files so they always
match correctly.

### Step 1 — Cleaning

First, the raw name is scrubbed. The app removes:

- **URLs** that captions sometimes start with.
- **Emoji** and decorative symbols (★, ➤, •, etc.).
- **Non-English characters** (replaced with spaces).
- **Telegram channel tags** like `@MyChannel_`.
- **Codec/source clutter** in the title area (e.g. `AMZN`, `DDP`, `AAC`, `5.1`, `x` bitrate tags).

```{admonition} Beginner Tip
:class: tip
This is why a messy caption like `@Movies4u ★ Ghosted 2023 720p WEBRip [AAC].mkv`
still matches correctly — the junk is stripped before matching.
```

### Step 2 — Parsing

The cleaned name is then analyzed to pull out:

- **Title** (e.g. *Ghosted*)
- **Year** (e.g. *2023*)
- **Season** and **Episode** numbers (for shows)
- **Quality / resolution** (e.g. *720p*, *1080p*, *2160p*)

The app uses two parsers together (a torrent-name parser plus a fallback) so it handles
a very wide range of naming styles.

```{admonition} Two hard requirements
:class: warning
A file is **only indexed** if the app can find **both**:

1. A **title**, and
2. A **quality/resolution** (like `720p` or `1080p`).

If either is missing, the file is skipped. Always include a resolution in your caption.
```

### Step 3 — Movie vs. TV decision

The rule is simple:

- If the name has a **season *and* episode** (e.g. `S01E04`) → treated as a **TV episode**.
- Otherwise → treated as a **Movie**.
- A **season with no episode** (e.g. just `S01`) → treated as a **whole-season "combined"** entry.

## Naming files correctly

### For movies

```text
Ghosted 2023 720p 10bit WEBRip [Org APTV Hindi AAC 2.0CH + English 6CH] x265 HEVC.mkv
```

Required: **Title**, **Year**, **Quality**. Everything else is optional extra info.

### For TV shows

```text
Harikatha.Sambhavami.Yuge.Yuge.S01E04.1080p.WEB-DL.DUAL.DDP5.1.H.264.mkv
```

Required: **Title**, **Season** (`S01`), **Episode** (`E04`), **Quality**.

### Good vs. bad names

```{list-table}
:header-rows: 1
:widths: 50 15 35

* - Filename / caption
  - Result
  - Why
* - `Inception 2010 1080p BluRay.mkv`
  - ✅ Good
  - Title, year, and quality all present
* - `The Office S02E05 720p WEB-DL.mkv`
  - ✅ Good
  - Clear season/episode and quality
* - `Ghosted.mkv`
  - ❌ Skipped
  - No quality/resolution
* - `movie_final_v2.mkv`
  - ❌ Skipped
  - No recognizable title or quality
* - `S01E01.mkv`
  - ❌ Skipped
  - No title
```

```{admonition} Beginner Tip
:class: tip
Put the details in the **caption** if the file name itself is ugly. The app prefers the
caption over the raw file name.
```

## Split files (`.001`, `.002`, …)

Large videos are sometimes uploaded in numbered pieces. The app detects a trailing
**`.ext.NN`** pattern — a video extension followed by a 2–3 digit number — and treats
those pieces as **parts of one video**.

```text
BigMovie.2023.1080p.mkv.001
BigMovie.2023.1080p.mkv.002
BigMovie.2023.1080p.mkv.003
```

What happens:

- The `.001` / `.002` suffix is **stripped before parsing**, so the numbers aren't
  mistaken for an episode number.
- All parts are grouped together (by channel + quality + base name).
- Stremio sees **one stream** and plays the parts back-to-back seamlessly.

```{admonition} Common Mistake
:class: warning
Upload the parts **to the same channel** and keep the base name identical. If the base
names differ, the app can't group them.
```

## Combined episodes and season packs

Sometimes one file contains several episodes, or a whole season.

The app detects patterns like:

- **Episode ranges** — `E01-E04`, `EP01~EP04`, `E01 to E04`, `E1+E2`.
- The keyword **`combined`** together with a season number (e.g. `S02 Combined`).
- A **season with no episode** (e.g. `S01`) is treated as a whole-season file.

Combined files are filed under a special **"Season N Combined"** entry (in a Specials
folder), labeled with the range like `E01-E04` or `Full`, so they don't clash with your
individual episodes.

```{admonition} Good to Know
:class: note
Valid episode ranges are between 1 and 99, and the start must be lower than the end
(e.g. `E01-E04` works; `E04-E01` does not).
```

## Automatic metadata (posters, descriptions, ratings)

Once a title and year are known, the app looks up rich details:

1. It first searches **IMDb / Cinemeta** for a strong match.
2. If that's weak or missing, it falls back to **TMDB** (needs your TMDB API key).
3. For channels you mark as **anime**, it tries anime-specific matching first.

From the match it stores: **poster**, **backdrop**, **logo**, **description**, **cast**,
**genres**, **runtime**, **rating**, and (for shows) **per-episode titles and stills**.

```{admonition} Beginner Tip
:class: tip
No poster showing? It usually means the title/year didn't match a database entry. Fix the
caption or use a manual override (below). A TMDB API key dramatically improves matching.
```

## Fixing incorrect or missing metadata

If a title is matched to the wrong movie/show, you have three easy options.

### Method 1 — IMDb / TMDB URL in the caption

1. Copy the correct **IMDb** or **TMDB** URL.
2. **Edit** the file's caption in your AUTH channel and paste the URL anywhere in it.
3. The app removes the old entry, re-scans using that URL, and saves the correct data.

### Method 2 — Scan Metadata in the web panel

1. Open the title under **Movies** or **TV Shows** in the panel.
2. Click **Edit → Scan Metadata**.
3. Search for the correct title and pick the right result.
4. Apply. The catalog updates instantly.

### Method 3 — `/set` for a batch of uploads

Send `/set <imdb-url>` to the bot, then forward the related files — they'll all use that
ID. Send `/set` with no URL to clear it afterward. See {doc}`admin-panel` for bot commands.

## Manual entry (add files by hand)

Some content isn't a public movie or show — personal videos, lectures, recordings. You can
add these manually from the panel instead of relying on automatic matching.

The manual-add flow (**Media Management → Manual Add**) lets you point at a specific
Telegram message and add it as a custom movie, show, season, episode, or extra quality.

You can identify the source message by:

- A **public link** — `t.me/<channel>/<message_id>`
- A **private link** — `t.me/c/<internal_id>/<message_id>`
- Or a **chat ID + message ID** pair.

When you provide the message, the app:

- Fetches the file and reads its real details.
- Determines quality from the **actual video height** when available
  (e.g. 1080 px → `1080p`), falling back to the filename.
- Uses the caption over the raw file name, and strips any split-part suffix.
- Records the original upload date/year.

```{admonition} Beginner Tip
:class: tip
Manual entry is perfect for **lectures, home videos, or anything without an IMDb page.**
You supply the title and details, and it streams just like everything else.
```

```{admonition} Good to Know
:class: note
Channels you designate as **manual channels** are *not* auto-indexed — they're reserved
for hand-added content, so random uploads there won't clutter your catalog.
```

## Auto-cleanup

The app keeps your library tidy automatically:

- **Delete a message** in Telegram → its catalog entry is purged automatically.
- **Edit a caption** with an override ID → the title is re-indexed.
- **Replace Mode** on → a new file of the same quality replaces the old one instead of
  creating a duplicate.
