# Catalogs

A **catalog** is a group of titles shown as a row/shelf in Stremio — like a playlist that
groups similar movies and shows together.

```{admonition} Beginner Tip
:class: tip
Think of a catalog like a **playlist** or a **shelf**: "Bollywood," "Anime," "Top Rated,"
or your own custom "Weekend Picks."
```

There are three kinds: **default**, **automatic**, and **custom/personal**.

## Default catalogs

Out of the box, your library is browsable by the standard **latest / popular** rows for
Movies and TV Shows, with genre and search filters supplied to Stremio.

## Automatic catalogs

The app can **automatically sort every title** into smart catalogs based on its metadata.
You choose which ones are enabled from **Catalogs → Auto Catalog Settings**.

The available automatic catalogs are grouped:

```{list-table}
:header-rows: 1
:widths: 20 80

* - Group
  - Catalogs
* - **Language**
  - Bollywood, Hollywood, Anime, K-Drama, Bengali, South Indian, Tamil, Telugu, Malayalam, Kannada, Japanese, Korean
* - **Smart**
  - Top Rated, Recently Added
* - **OTT / Platform**
  - Netflix, Prime Video, Hotstar, Apple TV, Hulu, HBO, JioCinema, ZEE5, SonyLIV, MX Player, Crunchyroll
```

### How auto-classification works

For each title, the app looks at data from TMDB and applies rules like:

- **Language → catalog.** Original language `hi` → Bollywood, `ja` → Japanese, `ko` →
  Korean, and so on.
- **Region nuance.** A Hindi title from India → **Bollywood**. A Korean **series** →
  **K-Drama**. A Japanese **animation** → **Anime**.
- **Ratings.** A rating of **7.5 or higher** → **Top Rated**.
- **Freshness.** Released in the **last year or so** → **Recently Added**.
- **Where it streams.** Its official streaming platforms (in your region) map to OTT
  catalogs like Netflix, Prime Video, or Hotstar.

```{admonition} Needs a TMDB key
:class: warning
Language and OTT catalogs require a **TMDB API key** (the app looks up language and
streaming-provider data). **Without a key, only "Top Rated" and "Recently Added" populate.**
```

### When does it run?

- **Instantly** for each new title as it's added (a quick per-item categorization).
- **In bulk** when you run a sync from the panel (processes your whole library in
  batches, reusing already-classified titles to stay fast).

```{admonition} Good to Know
:class: note
The default region for streaming-provider lookups is **India (IN)**, falling back to the
US. This affects which OTT catalog a title lands in.
```

## Custom catalogs

You can build your own catalogs by hand from **Catalogs → Custom Catalogs**.

- **Create** a catalog with any name (e.g. "Weekend Picks," "Kids").
- **Add / remove** titles individually.
- **Edit** the name and settings.
- **Delete** it when you're done.

## Personal / visibility controls

Each catalog has a **visibility** setting that controls who sees it in Stremio:

**Owner only**
: Visible just to you.

**Selected users**
: Visible only to specific access tokens you choose.

**Public**
: Visible to everyone using your addon.

```{admonition} Beginner Tip
:class: tip
Use **Selected users** to give friends or paying subscribers their own private shelves
without exposing them to everyone.
```

## Tags

Titles carry tags (such as **Netflix**, **Prime**, **Anime**, **Hindi**, **English**)
derived during classification. These tags are what power the automatic catalogs and help
group content consistently across your library.
