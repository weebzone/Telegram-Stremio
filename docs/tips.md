# Tips & Best Practices

Small habits that keep your server fast, tidy, and reliable.

## Name files well

Good captions are 90% of a smooth experience.

- Always include **title + year + resolution** for movies.
- Use `S01E04`-style markers for episodes.
- Let the app clean the junk — you don't need to strip tags yourself, but a clean caption
  matches faster.

```{admonition} Recommended
:class: tip
`Movie Name 2023 1080p WEB-DL.mkv` and `Show Name S01E05 720p.mkv` are the sweet spot —
enough for perfect matching, nothing missing.
```

## Keep metadata clean

- Add a **TMDB API key** early — it improves matching and unlocks language/OTT catalogs.
- Fix wrong matches promptly with a caption URL override or **Scan Metadata**.
- Run **Dead Links** cleanup occasionally to remove entries whose files were deleted.

## Backup regularly

- Export your **config JSON** before big changes or migrations.
- Snapshot your **MongoDB** databases on a schedule. See {doc}`updating`.

## Performance tuning

- Add extra **bot tokens** if several people stream at once — it spreads the load.
- Prefer **direct streaming** (no proxy) unless you specifically need a proxy for
  geo/IP reasons; a slow proxy adds buffering.
- Put the app behind a **reverse proxy with HTTPS** for the most compatible playback.
- Enable **Replace Mode** so you don't accumulate duplicate qualities.

## Recommended server specs

```{list-table}
:header-rows: 1
:widths: 30 35 35

* - Use case
  - CPU / RAM
  - Notes
* - Personal (1–2 viewers)
  - 1 vCPU / 1 GB
  - Free tiers or a small VPS are fine
* - Small group (a few viewers)
  - 1–2 vCPU / 2 GB
  - Add 1–2 extra bot tokens
* - Community (many viewers)
  - 2+ vCPU / 4 GB+
  - Several bot tokens + good bandwidth matter most
```

```{admonition} Good to Know
:class: note
Streaming is limited far more by **bandwidth and Telegram rate limits** than by CPU/RAM.
Prioritize a host with good network throughput.
```

## Storage growth

- When a storage database nears its size limit, **add another** from settings.
- Only remove storage databases from the **end** of the list to avoid breaking references.
