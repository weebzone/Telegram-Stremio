# Streaming

This page explains **how a file in Telegram becomes something you can watch** — in
plain English — plus the file formats supported and the analytics you can see.

## How streaming works

```text
   Telegram            PyroFork             FastAPI              Stremio
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Your video    │──▶ │ Reads bytes   │──▶ │ Serves them   │──▶ │ Plays the     │
│ sits here     │    │ on demand     │    │ over HTTP     │    │ video for you │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

Here's the same thing in words:

1. Your video **stays in Telegram** — it is never copied or re-uploaded.
2. When you press play, the app asks Telegram for the video **piece by piece**.
3. Those pieces are handed to your player over a normal web link.
4. The player shows the video as it arrives, so it **starts almost instantly** and you
   can seek/skip around.

```{admonition} Beginner Tip
:class: tip
Nothing is downloaded to the server first. It's like a straw into your Telegram file —
the app sips the exact part of the video you're currently watching and passes it along.
```

## Why links never expire

Instead of storing a temporary Telegram download link, the app stores the **channel ID**
and **message ID** of your file. Whenever someone plays it, a fresh connection to
Telegram is made on the spot. That's why your links keep working forever.

## Multiple bot tokens (load balancing)

Under heavy use, a single bot can only pull so much at once. If you add extra bot tokens
(see {doc}`configuration`), the app spreads active streams across all of them — like
opening more checkout lanes. Each viewer gets assigned to the least-busy bot.

## Supported file formats

The app recognizes these video types when reading captions/filenames:

```text
mkv   mp4   avi   ts   m4v   mov   wmv   webm   flv   m2ts   mpg   mpeg
```

It also understands **split archive parts** (files ending in `.001`, `.002`, …) so large
videos uploaded in pieces can be streamed as one. See {doc}`media-management` for the
naming rules.

```{admonition} Limitations
:class: warning
- The most reliable playback formats are **MKV** and **MP4** — stored titles are
  normalized to one of these.
- Some players struggle with certain codecs (e.g. exotic audio tracks). If a video plays
  with no sound, it's usually a codec issue in the player, not the server.
- "Multipart" files meant to be joined *outside* the player (e.g. `movie.part1.rar`) are
  **skipped** — they aren't streamable video on their own.
```

## Streaming analytics

The web panel and API expose live streaming telemetry so you can see what's happening.

**Active streams**
: Who/what is playing **right now**, including how many bytes have been transferred.

**Recent history**
: A rolling list of recently finished streams.

**Speed & health**
: A **Health Dashboard** and a **Speed Test** let you check that Telegram, your databases,
  and streaming are all responsive.

Relevant endpoints (see {doc}`api-reference` for details):

- `/stream/stats` — live and recent stream summary.
- `/stream/stats/{stream_id}` — detailed telemetry for one stream.
- `/api/system/speedtest` — measure real streaming throughput.

```{admonition} Good to Know
:class: note
If you enabled **subscriptions** or **per-token limits**, streaming usage (bandwidth) is
tracked per user token so daily/monthly caps can be enforced.
```
