---
title: Telegram Stremio
emoji: 🎬
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8000
pinned: false
---

<p align="center">
  <img src="https://iili.io/KhN0ztj.png" alt="Logo" width="400"/>
</p>

<p align="center">
  A powerful, self-hosted <b>Telegram Stremio Media Server</b> built with <b>FastAPI</b>, <b>MongoDB</b>, and <b>PyroFork</b> — stream your Telegram files directly inside <b>Stremio</b> / <b>Nuvio</b> with automatic metadata, catalogs, and permanent links.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/UV%20Package%20Manager-2B7A77?logo=uv&logoColor=white" alt="UV Package Manager" />
  <img src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/MongoDB-47A248?logo=mongodb&logoColor=white" alt="MongoDB" />
  <img src="https://img.shields.io/badge/PyroFork-EE3A3A?logo=python&logoColor=white" alt="PyroFork" />
  <img src="https://img.shields.io/badge/Stremio-8D3DAF?logo=stremio&logoColor=white" alt="Stremio" />
  <img src="https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white" alt="Docker" />
</p>

---

## 🧭 Quick Navigation

- [🚀 Introduction](#-introduction)
- [✨ Features Explained](#-features-explained)
  - [⚡ Instant Streaming & Permanent Links](#-instant-streaming--permanent-links)
  - [🧩 Split File Support](#-split-file-support)
  - [🎬 Combined / Multi-Episode File Support](#-combined--multi-episode-file-support)
  - [📚 Catalog Support (Auto + Custom)](#-catalog-support-auto--custom)
  - [🙋 Manual Upload — Personal Files & Lectures](#-manual-upload--personal-files--lectures)
  - [🍥 Anime Channel Support](#-anime-channel-support)
  - [🌐 Global File Support (Global Search)](#-global-file-support-global-search)
  - [🏷️ Metadata & Manual Override](#️-metadata--manual-override)
  - [💳 Subscription & Access Control](#-subscription--access-control)
- [📤 Upload Guidelines (Caption Format)](#-upload-guidelines-caption-format)
- [🤖 Bot Commands](#-bot-commands)
- [🔧 Configuration & Variable Guide](#-configuration--variable-guide)
  - [🪜 The 7 Startup Variables (config.env)](#-the-7-startup-variables-configenv)
  - [🎛️ Web Settings (everything else)](#️-web-settings-everything-else)
- [🚀 Deployment Guide](#-deployment-guide)
- [📺 Setting Up Nuvio Properly](#-setting-up-nuvio-properly)
- [🏅 Credits](#-credits)

---

## 🚀 Introduction

This project turns your **Telegram channels into a streaming backend for Stremio / Nuvio**. Forward a movie or episode to your channel and the bot indexes it, fetches metadata (poster, title, cast, ratings), and generates a **permanent streaming link** — no third-party hosts, no expiring links, no re-uploads.

It is built for **speed, scale, and reliability**, and works equally well for a personal library or a large community server.

---

## ✨ Features Explained

Below is a plain-English explanation of every major feature — **what it is, why it exists, and how to use it**.

### ⚡ Instant Streaming & Permanent Links

**What:** Every file you index gets a permanent URL that plays directly in Stremio/Nuvio.

**Why:** Telegram file links normally expire and other bots re-download files before serving them (slow, wasteful). This server streams the bytes **straight from Telegram on demand** through PyroFork + FastAPI, so playback starts fast and links never die.

**How:** Forward a file to your **AUTH channel** → it appears in your catalog → press play. That's it. A **multi-token load balancer** (see [Web Settings](#️-web-settings-everything-else)) spreads heavy traffic across multiple bot tokens for smooth playback under load.

---

### 🧩 Split File Support

Large files are often uploaded to Telegram in **parts**. This server can stitch the supported kind back together into one seamless stream — with correct seeking (⏪ / ⏩) and metadata.

#### ✅ Supported — `.001`, `.002`, `.003`, …

These are byte-parts of **one single original file** (e.g. `Movie.2024.1080p.mkv.001`, `...mkv.002`). When joined they reconstruct the exact original MKV container, keeping the same metadata and timeline. The server streams them as **one continuous video**, so seeking works perfectly.

#### ❌ Not Supported — `part001.mkv`, `part002.mkv`, …

Each of these is a **separate, independent MKV container** with its own metadata, timestamps, and timeline. Because they are individual videos, they cannot be merged into a single continuous playback timeline — so this naming style is not supported.

#### 🤔 What about `.zip.001`, `.zip.002`?

Technically these *could* be merged (they're parts of one ZIP archive), but support was **intentionally left out**. To stream from them the server would have to:

1. Download **all** ZIP parts
2. Merge them into one archive
3. Extract the video
4. Only *then* start streaming

That means ⏳ long startup delays, 💾 heavy temporary storage (ZIP **plus** the extracted video), and 📈 high CPU/disk usage that doesn't scale.

The whole point of this project is **fast, on-demand streaming without downloading/extracting first** — so `.zip.001` support was deliberately skipped. 🚀

> **TL;DR:** Upload split files as `filename.mkv.001`, `filename.mkv.002`, … and they'll play as one video.

---

### 🎬 Combined / Multi-Episode File Support

**What:** Files that pack several episodes (or a whole season) into a single video — e.g. `Show S01 [E04-06]`, `Show S01E01-E10`, or a "combined" season file — are now indexed and playable directly.

**Why:** Uploaders frequently share episode batches in one file. Previously these had nowhere clean to live. Now they're grouped so they show up and play in Stremio/Nuvio.

**How it appears:** Combined files are filed under a **special "Specials" section (Season 0)** with a clear label like **`Season 1 Combined`**, and the quality tag shows the range (e.g. `1080p E04-E06`, or `1080p Full` for a whole-season file). Just open the show and look under **Specials / Season 0**.

**Steps:**
1. Upload the combined file to your **AUTH channel** with a clear caption (see [Upload Guidelines](#-upload-guidelines-caption-format)).
2. Open the series in Stremio/Nuvio → go to the **Specials (Season 0)** row → play.

> ⚠️ **Limitation:** Combined files do **not** appear in [Global Search](#-global-file-support-global-search) yet.
>
> 💡 **Tip:** For combined files to show correct metadata reliably, keep the **Telegram Stremio addon above Cinemeta** — see [Setting Up Nuvio Properly](#-setting-up-nuvio-properly).

---

### 📚 Catalog Support (Auto + Custom)

Catalogs are the **rows** you see in Stremio/Nuvio (e.g. "Bollywood", "Netflix", "My Lectures"). There are two kinds:

#### 🤖 Auto Catalogs

**What:** The server can automatically sort your library into ready-made rows.

- **Language:** Bollywood, Hollywood, Anime, K-Drama, Bengali, South Indian, Tamil, Telugu, Malayalam, Kannada, Japanese, Korean
- **Smart:** Top Rated, Recently Added
- **OTT / Platform:** Netflix, Prime Video, Hotstar, Apple TV, Hulu, HBO, JioCinema, ZEE5, SonyLIV, MX Player, Crunchyroll

**Why:** Zero manual work — your catalog organizes itself as you add files.

**How:** Go to the auto-catalog settings, tick the rows you want, and save. New uploads are categorized instantly.

> ℹ️ **Language & OTT** rows need a **TMDB API Key** to classify correctly. **Top Rated** and **Recently Added** work without one.

#### ✍️ Custom Catalogs

**What:** Rows **you** create and fill by hand (e.g. "4K Movies", "My Course", "Private").

**Why:** Full control over grouping and, importantly, **who can see each row**.

**How:** Create a catalog, choose its **visibility**, then add titles to it:

| Visibility | Who sees it |
| :--- | :--- |
| **Everyone** (`public`) | All users who installed the addon |
| **Specific users** (`tokens`) | Only the tokens/users you pick |
| **Owner only** (`owner`) | Only you (hidden from others) |

Two extra per-catalog toggles:

- **🔒 Only visible in this catalog** — removes those titles from the Default, Auto, and every other catalog, so they appear **only** in this one.
- **🔎 Allow search** — lets allowed users find those titles via search (off by default for private catalogs).

---

### 🙋 Manual Upload — Personal Files & Lectures

**What:** Add your **own** files — lectures, personal videos, or any private media that isn't a public Movie/TV Show — without them being auto-indexed or exposed publicly.

**Why:** A **Manual Channel** is treated differently from an AUTH channel: files sent there are **not** auto-scanned against TMDb/IMDb and won't leak into public catalogs. You add each entry by hand and control exactly who sees it. Perfect for 🎓 lectures, 🎥 personal videos, and 📁 any private media.

#### 📋 Step-by-step

1️⃣ **Add a Manual Channel**
&nbsp;&nbsp;&nbsp;• Go to **Settings → Manual Add Channels** and add your channel.

2️⃣ **Assign Telegram User IDs**
&nbsp;&nbsp;&nbsp;• Go to **Access** and assign the TG User ID(s) to each token that should reach the files.

3️⃣ **Create a Custom Catalog**
&nbsp;&nbsp;&nbsp;• Create a new catalog (name it anything) and set **Visibility** to **👤 Owner only** or **👥 Specific users**.

4️⃣ **Create a Manual Entry**
&nbsp;&nbsp;&nbsp;• In **Media Management**, add your file as a **Manual Entry** (paste the Telegram message link). Fill in the correct title/metadata.

5️⃣ **Add It to Your Catalog**
&nbsp;&nbsp;&nbsp;• Add the manual entry to the custom catalog from Step 3.

#### 🔒 Who can see it?

Only the **Owner** or the **assigned token(s)** can access these personal files.

#### 📂 Hide it from default catalogs

By default a title can appear in both the **Default catalog** and **your Custom catalog**. To keep it **only** in yours:

➡️ Open **Catalog Settings** → enable **🔒 Only visible in this catalog**. It's now hidden from all default/auto catalogs.

#### 🔍 Make it searchable (optional)

Personal files do **not** show in search by default. To allow it:

➡️ Open **Catalog Settings** → enable **🔎 Allow search**. Your files now appear when searched in Nuvio.

---

### 🍥 Anime Channel Support

**What:** Any AUTH channel can be flagged as an **Anime channel** so its files are matched against **AniList + api.ani.zip** instead of standard movie databases.

**Why:** Anime seasons, episode numbering, and titles (romaji/english/synonyms) rarely match cleanly on TMDb/IMDb. AniList gives far more accurate anime metadata — correct titles, episode names, posters, and mappings.

**How:** In **Settings → AUTH_CHANNELS**, tick the **Anime** checkbox next to the channel that holds your anime. Files uploaded there are indexed with anime-aware metadata automatically.

---

### 🌐 Global File Support (Global Search)

**What:** Search **across other Telegram channels** you don't index locally, and stream matching results on the fly. Results that aren't in your local catalog are tagged **🌐 GLOBAL** in Stremio/Nuvio.

**Why:** You can't (and shouldn't) permanently index every channel. Global Search lets you pull in a title on demand from selected source channels, using your own Telegram account (Userbot).

**How to enable:**
1. Set **`USER_SESSION_STRING`** in `config.env` (see the [Variable Guide](#-the-7-startup-variables-configenv)) and **restart once**.
2. In **Settings → Global Search**, enable the toggle and add the **channel IDs** to search.

#### ❓ Why don't all my auth-channel files show up in Global Search?

Global Search is a **live title match**, not a dump of a channel. A file only surfaces when:
- Its filename **matches the requested title** (and season/episode, if any) closely enough, **and**
- It's a **normal single video** — [split files](#-split-file-support) and [combined files](#-combined--multi-episode-file-support) are **skipped** in Global Search on purpose.

So a file already sitting in your AUTH channel appears through your **normal catalog** (where it's fully indexed), not necessarily through Global Search. Global Search is meant for reaching titles **outside** your indexed library.

#### ❓ How do I get catalogs/metadata for movies & shows that aren't in my auth channel?

Use the **Cinemeta** addon for discovery — **but keep it *below* Telegram Stremio** in your addon order. That way:
- Metadata and browse rows come from Cinemeta for titles you don't host, **and**
- Whenever *you* have the file, your Telegram Stremio stream takes priority.

See [Setting Up Nuvio Properly](#-setting-up-nuvio-properly) for the exact ordering.

---

### 🏷️ Metadata & Manual Override

If the addon identifies a title incorrectly or metadata is missing, fix it in one of two ways:

**Method 1 — IMDb/TMDb URL in caption:** Edit the file's caption in your AUTH channel and paste the correct IMDb or TMDb URL anywhere in it. The bot removes the old entry, re-scans the URL, and saves the correct metadata.

**Method 2 — Scan from the Web Panel:** Open the entry in Movies/TV Shows → **Edit** → **Scan Metadata** → search and pick the right title → apply.

#### 🔁 Quality Replacement (Replace Mode)

> Works only when **Replace Mode** is enabled.

If a new file has the **same quality label** (`720p`, `1080p`, `4K`, …) as an existing one, it **replaces** the old entry — no duplicates. This is also how you upgrade a **CAMRip/low-quality** file: just forward the better version and it swaps in automatically. No commands, no manual deletion.

---

### 💳 Subscription & Access Control

**What (optional):** Gate streaming behind a subscription. Each user gets a **unique addon token**; admins approve payments from the bot, and the addon manifest reflects each user's status/expiry.

**Why:** Monetize or restrict access to a community server.

**How:** Enable it in **Settings → Subscription**, set the **Subscription Group ID**, **Payment Instructions**, optional **QR image**, and **Approver IDs**. Manage users (assign/extend/reduce/revoke) from **Admin → Access Management**. Expired or not-joined users see a single actionable entry that opens **your bot** (its username is detected automatically — nothing to configure).

---

## 📤 Upload Guidelines (Caption Format)

Accurate metadata depends on a clean caption (or filename). Include these fields:

**🎥 Movies**
```
Ghosted 2023 720p 10bit WEBRip [Org APTV Hindi AAC 2.0CH + English 6CH] x265 HEVC Msub ~ PSA.mkv
```
- 🎞️ **Name** (e.g. `Ghosted`) · 📅 **Year** (e.g. `2023`) · 📺 **Quality** (e.g. `720p`)

**📺 TV Shows**
```
Harikatha.Sambhavami.Yuge.Yuge.S01E04.Dark.Hours.1080p.WEB-DL.DUAL.DDP5.1.Atmos.H.264-Spidey.mkv
```
- 🎞️ **Name** · 📆 **Season** (`S01`) · 🎬 **Episode** (`E04`) · 📺 **Quality** (`1080p`)

**📦 Combined files** — include the season and episode range, e.g. `Show.S01.E04-E06.1080p.mkv` or a "combined" keyword for whole-season files.

✅ Optional extras (codec, audio, source) like `WEB-DL`, `x265`, `Dual Audio` are fine.

---

## 🤖 Bot Commands

| Command | Description |
| :--- | :--- |
| **`/start`** | Returns your **Addon URL** for installing in Stremio/Nuvio. |
| **`/log`** | Sends the latest **log file** for debugging. |
| **`/set <imdb-url>`** | Manually upload a specific title by linking IMDb metadata. |
| **`/restart`** | Restarts the bot and pulls the latest upstream updates. |

**`/set` usage:** send `/set https://www.imdb.com/title/tt0468569/`, forward the related files, then send `/set` alone to clear the linked ID.

---

## 🔧 Configuration & Variable Guide

Setup has **two layers**:

| Layer | Where | When | What |
| :--- | :--- | :--- | :--- |
| 🧱 **Startup** | `config.env` | Once, before first launch | The core credentials to boot |
| 🎛️ **Runtime** | **Web Settings page** | Anytime after launch | Everything else — saved to DB, applied live |

### 🪜 The 7 Startup Variables (config.env)

Copy the sample and edit it: `cp sample_config.env config.env && nano config.env`

| Variable | Required | What it is & why you need it |
| :--- | :---: | :--- |
| `API_ID` | ✅ | Your Telegram **API ID** from [my.telegram.org](https://my.telegram.org). Identifies your Telegram app so the bot can connect. |
| `API_HASH` | ✅ | Your Telegram **API Hash** from the same page. The secret paired with `API_ID`. |
| `BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather). This is the account that indexes files and serves streams — **make it an admin in every channel** you use. |
| `OWNER_ID` | ✅ | Your numeric Telegram user ID (from [@userinfobot](https://t.me/userinfobot)). Grants you owner-level control. |
| `DATABASE` | ✅ | **Two** MongoDB connection URIs, comma-separated. The **first** stores tracking/metadata, the **second** stores media references. Free **M0** Atlas tier is enough; you can reuse one cluster with two db names (e.g. `/tracking` and `/storage1`). More storage can be added later from Web Settings. |
| `PORT` | ✅ | The web server port. Keep `8000` unless it's in use; your reverse proxy/domain points here. |
| `USER_SESSION_STRING` | ⬜ | **Optional** — only needed for [Global Search](#-global-file-support-global-search). A "stay logged in" token for **your own** Telegram account (like Telegram Web). It lets the Userbot search other channels. Revoke anytime from Telegram → Settings → Devices. Leave empty if you don't use Global Search. |

**Example `config.env`:**
```env
API_ID="1234567"
API_HASH="abc123def456ghi789jkl012mno345pq"
BOT_TOKEN="1234567890:AAEabcdEFGhijkLMnOPqrsTUVwxyz12345"
USER_SESSION_STRING=""
OWNER_ID="987654321"
DATABASE="mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/tracking,mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/storage1"
PORT="8000"
```

> **Generate `USER_SESSION_STRING`** (only for Global Search): open [colab.new](https://colab.new), run a cell with `!pip install pyrogram tgcrypto`, then use Pyrogram's `export_session_string()` after logging in with your API ID/HASH + login code. Keep the result **private** — never commit it.

### 🎛️ Web Settings (everything else)

Once running, open your site → log in at `/login` with the default `admin` / `admin` and **change the password immediately**. Then open **Settings** (`/admin/settings`). All values below save to the database and apply **instantly, no restart** (the only exception is `USER_SESSION_STRING`, which lives in `config.env`).

| Setting | What it does & why |
| :--- | :--- |
| **Replace Mode** | When a new file matches an existing quality, it replaces the old one. Keeps catalogs clean. Recommended **ON**. |
| **Hide Catalog** | Hides the public catalog while direct streams still work. |
| **Admin Username / Password** | Your web panel login. **Change the defaults right away.** |
| **AUTH_CHANNELS** | The channel(s) the bot indexes and streams from. Add by `@username` or `-100…` ID; the bot must be **admin**. Tick **Anime** on a channel to use AniList metadata (see [Anime Support](#-anime-channel-support)). |
| **Manual Add Channels** | Channels whose files are **not** auto-indexed — used for [manual/personal uploads](#-manual-upload--personal-files--lectures). |
| **TMDB API Key** | Free TMDB **v3** key from themoviedb.org. Powers metadata matching and Language/OTT [auto catalogs](#-catalog-support-auto--custom). |
| **Base URL** | Your public address (e.g. `https://your-domain.com`). Stremio uses this to reach your streams — **must be correct**. |
| **Upstream Repo / Branch** | Optional — used by `/restart` to auto-update. |
| **Subscription** | Optional monetization/access gating — see [Subscription](#-subscription--access-control). |
| **Global Search** | Requires `USER_SESSION_STRING` + one restart. Enable the toggle and add channel IDs to search — see [Global File Support](#-global-file-support-global-search). |
| **HTTP Proxy** | Optional proxy for outbound metadata/API requests; can show both proxied and direct links. |
| **Extra Storage Databases** | Add more MongoDB URIs to expand capacity. The first two (from `config.env`) are locked. Remove only from the **end** of the list. |
| **Multi-Token Clients** | Add extra **bot tokens** for faster parallel streaming under load. Add each bot as **admin** in your AUTH channels. |

> ✅ Click **Save Settings** and you're live.

---

## 🚀 Deployment Guide

Before you start (for VPS): a server with a public IP (Ubuntu on DigitalOcean/AWS/Vultr/etc.) and a **domain name**.

### 🐙 Heroku

Follow the guided Colab tool:

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/weebzone/Colab-Tools/blob/main/telegram%20stremio.ipynb)

### 🐳 VPS with Docker (Recommended)

**1. Clone & configure:**
```bash
git clone https://github.com/weebzone/Telegram-Stremio
cd Telegram-Stremio
mv sample_config.env config.env
nano config.env   # fill in the 7 variables, then Ctrl+O, Enter, Ctrl+X
```

**2. Start (Docker Compose — recommended):**
```bash
docker compose up -d
```
Server runs at `http://<your-vps-ip>:8000`. To apply `config.env` edits later: `docker compose restart` (no rebuild needed — the file is mounted).

**Or plain Docker:**
```bash
docker build -t telegram-stremio .
docker run -d -p 8000:8000 telegram-stremio
```

**3. Add a domain (HTTPS via Caddy):**

Point an **A record** for your domain at your VPS IP, then install Caddy:
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```
Edit `/etc/caddy/Caddyfile`:
```caddy
your-domain.com {
    reverse_proxy localhost:8000
}
```
Then `sudo systemctl reload caddy`. Your server is now live at `https://your-domain.com`.

### 🤗 Hugging Face (free, always-online)

1. **Fork** this repo, then create a **Docker Space** (Blank template, **Public** visibility) at [huggingface.co/new-space](https://huggingface.co/new-space).
2. In your Space → **Settings → Variables and secrets**, add the same values you'd put in `config.env` (`API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `DATABASE`, and optionally `USER_SESSION_STRING`). No `config.env` file is needed — they're read as environment variables.
3. Wait for the Space to build and show **Running**.
4. Open `https://<your-hf-username>-<your-space-name>.hf.space/login`, log in (`admin`/`admin`), **change the password**, then set your **Base URL** to that same `hf.space` address in Web Settings.
5. Fill in **TMDB API** and **AUTH CHANNEL** in Settings, save, then send `/start` to your bot for the manifest URL.

---

## 📺 Setting Up Nuvio Properly

Your server is a standard **Stremio-style addon**, so it works in Stremio too — but for the smoothest experience across devices we recommend **[Nuvio](https://play.google.com/store/apps/details?id=com.nuvio.app)** (free, open-source, for Android, Android TV, Fire TV, iOS, Windows, and TV). *(Content was rephrased for compliance with licensing restrictions.)*

### Step 1 — Install Nuvio

| Platform | Source |
| :--- | :--- |
| Android / Android TV / Fire TV | [Google Play](https://play.google.com/store/apps/details?id=com.nuvio.app) |
| All platforms / latest builds | [GitHub — tapframe/NuvioStreaming](https://github.com/tapframe/NuvioStreaming) |

### Step 2 — Add the addon

1. Send `/start` to your bot to get your **manifest URL**.
2. In Nuvio (or Stremio), open the **Addons** section and install that manifest URL.

### Step 3 — Order it **above Cinemeta** ⭐ (important)

For combined files, global files, and correct stream priority to work well, the **Telegram Stremio addon must sit above Cinemeta**:

1. Open the **Addons** section in Nuvio.
2. **Move Telegram Stremio to the top** — it must always be **above** the Cinemeta addon.
3. Keep **Cinemeta below it** so you still get browse rows/metadata for titles you don't host, while your own streams always take priority when you *do* have the file.

> 📺 **On Stremio**, you can reorder addons with the community tool: <https://addon-manager.dontwanttos.top/>

### Step 4 — Add & verify a manual entry

1. Add a **manual entry** on the web page (Media Management) and fill in its metadata.
2. Open the title in Nuvio — with Telegram Stremio on top, it will now show the **correct metadata you filled in** and stream from your channel.

---

## 🏅 Credits

| <img width="80" src="https://avatars.githubusercontent.com/u/113664541"> | <img width="80" src="https://avatars.githubusercontent.com/u/13152917"> | <img width="80" src="https://avatars.githubusercontent.com/u/14957082"> | <img width="80" src="https://raw.githubusercontent.com/vflixa1prime/Readme/main/VFlixPRime.png"> |
| :---: | :---: | :---: | :---: |
| [`Karan`](https://github.com/Weebzone) | [`Stremio`](https://github.com/Stremio) | [`ChatGPT`](https://github.com/OPENAI) | [`VFlix Prime`](https://t.me/vflixprime2) |
| Author | Stremio SDK | Refactor | Community Support |
