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
  A powerful, self-hosted <b>Telegram Stremio Media Server</b> built with <b>FastAPI</b>, <b>MongoDB</b>, and <b>PyroFork</b> — seamlessly integrated with <b>Stremio</b> for automated media streaming and discovery.
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

* [🚀 Introduction](#-introduction)

  * [✨ Key Features](#-key-features)
  * [💳 Subscription Management](#-subscription-management)
  * [📋 Subscription Plans](#-subscription-plans)
  * [🤖 Bot Payment Flow](#-bot-payment-flow)
  * [🗃️ Access Management](#️-access-management)
  * [🎬 Stremio Addon Integration](#-stremio-addon-integration)

* [⚙️ How It Works](#️-how-it-works)

  * [📖 Overview](#overview)
  * [📤 Upload Guidelines](#upload-guidelines)
  * [🔁 Quality Replacement Logic](#-quality-replacement-logic)
  * [🎥 Updating CAMRip or Low-Quality Files](#-updating-camrip-or-low-quality-files)
  * [⚙️ Behind the Scenes](#behind-the-scenes)

* [🤖 Bot Commands](#-bot-commands)

  * [📜 Command List](#command-list)
  * [⚙️ /set Command Usage](#set-command-usage)

* [🔧 Configuration Guide](#-configuration-guide)

  * [🪜 Step 1: Create Your config.env File](#-step-1-create-your-configenv-file)
  * [🔑 Step 2: How to Get Each Value](#-step-2-how-to-get-each-value)
  * [📱 Step 3: Generate Your Telegram Session String](#-step-3-generate-your-telegram-session-string)
  * [🧩 Step 4: Configure Everything Else (Web Settings Page)](#-step-4-configure-everything-else-web-settings-page)

* [🚀 Deployment Guide](#-deployment-guide)

  * [✅ Recommended Prerequisites](#-recommended-prerequisites)
  * [🐙 Heroku Guide](#-heroku-guide)
  * [🐳 VPS Guide (Recommended)](#-vps-guide-recommended)
  * [🤗 Hugging Face Guide](#-hugging-face-guide)

* [📺 Setting Up Your App (Nuvio Recommended)](#-setting-up-your-app-nuvio-recommended)

  * [📥 Install Nuvio](#-step-1-install-nuvio)
  * [🌐 Add the Addon](#-step-2-add-the-addon)

* [🏅 Contributors](#-contributors)


# 🚀 Introduction

This project is a **next-generation Telegram Stremio Media Server** that allows you to **stream your Telegram files directly through Stremio**, without any third-party dependencies or file expiration issues. It’s designed for **speed, scalability, and reliability**, making it ideal for both personal and community-based media hosting.


## ✨ Key Features

- ⚙️ **Multiple MongoDB Database Support**
- 📡 **Multiple Telegram Channel Support**
- ⚡ **Ultra-Fast Streaming Experience**
- 🔑 **Multi-Token Load Balancer**
- 🎬 **IMDb & TMDb Metadata Integration**
- 🧩 **Seamless Split File Streaming Support**
- 🎞️ **Play Multi-Part Videos as a Single Stream**
- ♾️ **Permanent Streaming Links (No Expiration)**
- 🧠 **Powerful Admin Dashboard**
- 💳 **Subscription & Premium Management**
- 🔐 **Advanced Access Control System**
- 📚 **Custom & Automatic Catalog Generation**
- 🖥️ **Web-Based Configuration Panel**
- 🌐 **Built-in Addon Proxy Support**
- 🔍 **Global Search Across Selected Channels**



## ⚙️ How It Works

This project acts as a **bridge between Telegram storage and Stremio streaming**, connecting **Telegram**, **FastAPI**, and **Stremio** to enable seamless movie and TV show streaming directly from Telegram files.

### Overview

When you **forward Telegram files** (movies or TV episodes) to your **AUTH CHANNEL**, the bot automatically:

1.  🗃️ **Stores** the `message_id` and `chat_id` in the database.
2.  🧠 **Processes** file captions to extract key metadata (title, year, quality, etc.).
3.  🌐 **Generates a streaming URL** through the **PyroFork** module — routed by **FastAPI**.
4.  🎞️ **Provides Stremio Addon APIs**:
    -   `/catalog` → Lists available media
    -   `/meta` → Shows detailed information for each item
    -   `/stream` → Streams the file directly via Telegram

### Upload Guidelines

To ensure proper metadata extraction and seamless integration with **Stremio**, all uploaded Telegram media files **must include specific details** in their captions.

#### 🎥 For Movies

**Example Caption:**

```
Ghosted 2023 720p 10bit WEBRip [Org APTV Hindi AAC 2.0CH + English 6CH] x265 HEVC Msub ~ PSA.mkv
```

**Required Fields:**

-   🎞️ **Name** – Movie title (e.g., _Ghosted_)
-   📅 **Year** – Release year (e.g., _2023_)
-   📺 **Quality** – Resolution or quality (e.g., _720p_, _1080p_, _2160p_)

✅ **Optional:** Include codec, audio format, or source (e.g., `WEBRip`, `x265`, `Dual Audio`).

#### 📺 For TV Shows

**Example Caption:**

```
Harikatha.Sambhavami.Yuge.Yuge.S01E04.Dark.Hours.1080p.WEB-DL.DUAL.DDP5.1.Atmos.H.264-Spidey.mkv
````

**Required Fields:**

-   🎞️ **Name** – TV show title (e.g., _Harikatha Sambhavami Yuge Yuge_)
-   📆 **Season Number** – Use `S` followed by two digits (e.g., `S01`)
-   🎬 **Episode Number** – Use `E` followed by two digits (e.g., `E04`)
-   📺 **Quality** – Resolution or quality (e.g., _1080p_, _720p_)

✅ **Optional:** Include episode title, codec, or audio details (e.g., `WEB-DL`, `DDP5.1`, `Dual Audio`).

### 🔁 Quality Replacement Logic

> Works only when **Replace Mode** is enabled.

If a newly uploaded file has the same quality label (`720p`, `1080p`, `4K`, etc.) as an existing file, the bot automatically replaces the older entry with the new one.

**Example:** Uploading a new `Ghosted (2023) 720p` file will replace the existing `720p` version in the catalog.

This prevents duplicate quality entries and ensures only the latest version is available for streaming.

---

### 🆙 Updating CAMRip or Low-Quality Files

> Works only when **Replace Mode** is enabled.

If you initially uploaded a **CAMRip or low-quality version**, you can easily replace it with a better one:

1. Forward the **new, higher-quality file** (e.g., `1080p`, `WEB-DL`) to your **AUTH CHANNEL**.
2. The bot will **automatically detect and replace** the old CAMRip file in the database.
3. The Stremio addon will then **update automatically**, showing the new stream source.

✅ No manual deletion or command is needed — forwarding the updated file is enough!

---

### 🏷️ Fixing Incorrect Metadata (Manual Override)

If the addon identifies a movie or TV show incorrectly, or if metadata is missing altogether, you can easily correct it using one of the following methods:

#### Method 1: IMDb / TMDb URL Override

1. Copy the correct **IMDb** or **TMDb** URL for the movie or TV show.
2. Edit the message caption in your Telegram **AUTH CHANNEL** and paste the URL anywhere in the caption.
3. The bot will automatically:

   * Remove the existing metadata entry associated with that file.
   * Re-scan the provided URL.
   * Fetch and save the correct metadata.

#### Method 2: Scan Metadata from the Web Panel

1. Open the media entry from the Movies or TV Shows section.
2. Click **Edit**.
3. Select **Scan Metadata**.
4. Search for the correct title and choose the matching result.
5. Apply the changes.

✅ The addon will update the metadata instantly and refresh the catalog entry.

---


### Behind The Scenes

Here's how each component interacts:

| Component | Role |
| :--- | :--- |
| **Telegram Bot** | Handles uploads, forwards, and file tracking. |
| **MongoDB** | Stores message IDs, chat IDs, and metadata. |
| **PyroFork** | Generates Telegram-based streaming URLs. |
| **FastAPI** | Hosts REST endpoints for streaming, catalog, and metadata. |
| **Stremio Addon** | Consumes FastAPI endpoints for catalog display and playback. |

📦 **Flow Summary:**

```
Telegram ➜ MongoDB ➜ FastAPI ➜ Stremio ➜ User Stream
```



# 🤖 Bot Commands

Below is the list of available bot commands and their usage within the Telegram bot.

### Command List

| Command | Description |
| :--- | :--- |
| **`/start`** | Returns your **Addon URL** for direct installation in **Stremio**. |
| **`/log`** | Sends the latest **log file** for debugging or monitoring. |
| **`/set`** | Used for **manual uploads** by linking IMDB URLs. |
| **`/restart`** | Restarts the bot and pulls any **latest updates** from the upstream repository. |

### `/set` Command Usage

The `/set` command is used to manually upload a specific Movie or TV show to your channel, linking it to its IMDB metadata.

**Command:**

```
/set <imdb-url>
```

**Example:**

```
/set https://www.imdb.com/title/tt0468569/
```

**Steps:**

1.  Send the `/set` command followed by the **IMDB URL** of the movie or show you want to upload.
2.  **Forward the related movie or TV show files** to your channel.
3.  Once all files are uploaded, **clear the default IMDB link** by simply sending the `/set` command without any URL.

💡 **Tip:** Use `/log` if you encounter any upload or parsing issues.



# 🔧 Configuration Guide

> 😌 **Don’t worry — setup is easier than it looks.**
> You only fill in **6 values once** inside a single file called `config.env`. Everything else (TMDB key, channels, admin login, subscriptions, proxy…) is configured later from a friendly **Web Settings page** — no code, no restarts.

Think of configuration as **two simple layers**:

| Layer | Where | When you set it | What goes here |
| :--- | :--- | :--- | :--- |
| 🧱 **Startup** | `config.env` file | Once, before first launch | The core credentials needed to boot |
| 🎛️ **Runtime** | **Web Settings page** | Anytime, after launch | Everything else — saved to the database, applied live |

---

## 🪜 Step 1: Create Your config.env File

After cloning the project, copy the sample file and open it for editing:

```bash
cp sample_config.env config.env
nano config.env
```

Fill in these values:

| Variable | Required | What it is |
| :--- | :---: | :--- |
| `API_ID` | ✅ | Telegram API ID (from my.telegram.org) |
| `API_HASH` | ✅ | Telegram API Hash (from my.telegram.org) |
| `BOT_TOKEN` | ✅ | Your bot token (from @BotFather) |
| `OWNER_ID` | ✅ | Your numeric Telegram user ID |
| `DATABASE` | ✅ | **Two** MongoDB URIs, separated by a comma |
| `PORT` | ✅ | Web server port (keep `8000` unless it’s busy) |
| `USER_SESSION_STRING` | ⬜ | Optional — only needed for **Global Search** |

A completed file looks like this (these are just example values):

```env
API_ID="1234567"
API_HASH="abc123def456ghi789jkl012mno345pq"
BOT_TOKEN="1234567890:AAEabcdEFGhijkLMnOPqrsTUVwxyz12345"
USER_SESSION_STRING=""
OWNER_ID="987654321"
DATABASE="mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/tracking,mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/storage1"
PORT="8000"
```

> 💾 To save in nano: press `Ctrl + O`, then `Enter`, then `Ctrl + X`.

---

## 🔑 Step 2: How to Get Each Value

Take it one line at a time — each value comes from a quick, free step.

### 🆔 API_ID & API_HASH
1. Go to **https://my.telegram.org** and log in with your phone number.
2. Open **API development tools**.
3. Create an app (any title works, e.g. `stremio`).
4. Copy **App api_id** → `API_ID` and **App api_hash** → `API_HASH`.

### 🤖 BOT_TOKEN
1. Open **@BotFather** in Telegram.
2. Send `/newbot` and follow the prompts (choose a name and a username).
3. Copy the token it gives you → `BOT_TOKEN`.
4. ⭐ Add this bot as an **admin** in every channel you’ll use for media.

### 👤 OWNER_ID
1. Open **@userinfobot** in Telegram (or send `/id` to **@MissRose_bot**).
2. It replies with your numeric ID → `OWNER_ID`.

### 🗄️ DATABASE (two MongoDB URIs)
You need **two** free MongoDB databases — the first stores tracking/metadata, the second stores your media references.

1. Create a free account at **https://www.mongodb.com/atlas** (the forever-free **M0** tier is enough to start).
2. Create a cluster → in **Database Access**, add a database user and password.
3. In **Network Access**, add `0.0.0.0/0` (allow access from anywhere).
4. Click **Connect → Drivers** and copy the connection string, e.g.
   `mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/`
5. Add a database name at the end of each (e.g. `/tracking` and `/storage1`).
6. Put **both** strings on one line, separated by a comma:
   ```
   DATABASE="mongodb+srv://.../tracking,mongodb+srv://.../storage1"
   ```

> 💡 You can use the **same cluster** for both — just give them two different database names. Need more space later? Add extra storage databases from the Web Settings page (no restart required).

### 🔢 PORT
Leave it as `8000` unless that port is already in use. Your reverse proxy / domain will point here.

### 📱 USER_SESSION_STRING (optional)
Only needed if you want **Global Search**. It’s safe and quick to generate — see **Step 3** below. If you don’t need Global Search, leave it empty.

---

## 📱 Step 3: Generate Your Telegram Session String

> 😊 **No app installation required — and it’s safe.**
> A session string is simply a “stay logged in” token for **your own** Telegram account, exactly like signing into Telegram Web. The bot never sees your password, and you can revoke access anytime from **Telegram → Settings → Devices**.

> ⏭️ **Skip this step** entirely if you don’t plan to use Global Search.

### 🌐 Easiest Method: Google Colab (works right in your phone’s browser)

1️⃣ Open **https://colab.new** in your browser.

2️⃣ Sign in with your Google account.

3️⃣ Tap **“+ Code”** to add a new code cell.

4️⃣ Paste the code below and press ▶ **Run**:

```python
!pip install pyrogram tgcrypto

import asyncio
from pyrogram import Client

api_id = int(input("API ID: "))
api_hash = input("API HASH: ")

async def main():
    async with Client("temp_session", api_id, api_hash) as app:
        print("\nYour USER_SESSION_STRING is:\n")
        print(await app.export_session_string())

await main()
```

5️⃣ Enter your **API ID** and **API HASH** when prompted.

6️⃣ Enter the **login code** Telegram sends you (and your 2-step password, if you have one).

7️⃣ Your **USER_SESSION_STRING** is printed on screen — copy the whole string into `config.env`.

> 🔒 **Keep it private.** Anyone who has this string can access your account, so never share it or commit it to a public repository. To invalidate it instantly, just remove the session from Telegram’s **Devices** list.

---

## 🧩 Step 4: Configure Everything Else (Web Settings Page)

Once the server is running, open it in your browser:

| Setup | Open this URL |
| :--- | :--- |
| **VPS with a domain** | `https://your-domain.com` |
| **Local / direct IP** | `http://<your-vps-ip>:8000` |

You’ll land on the **login page** (`/login`). Sign in with the default credentials:

```
Username: admin
Password: admin
```

Then go to **Settings** (`/admin/settings`).

> 🚨 **Do this first:** change the admin password in the **Admin Authentication** card, then click **Save Settings**.

Everything below is stored in the database and applied **instantly — no restart needed** (the only exception is `USER_SESSION_STRING`, which lives in `config.env`).

### ⚙️ General
| Option | What it does |
| :--- | :--- |
| **Replace Mode** | When a new file has the same quality (`720p`, `1080p`…) as an existing one, it replaces the old entry. Recommended **ON**. |
| **Hide Catalog** | Hides the public Stremio catalog (direct streams still work). |

### 🛡️ Admin Authentication
| Field | What to enter |
| :--- | :--- |
| **Admin Username / Password** | Your Web Panel login. Leave the password blank to keep the current one. **Change the defaults right away.** |
| **AUTH_CHANNELS** | The channel(s) the bot indexes and streams from. Add each one by `@username` or `-100…` ID. Make sure your bot is an **admin** in each channel. |

### 🎬 Media & Content
| Field | What to enter |
| :--- | :--- |
| **TMDB API Key** | A free TMDB **v3** key from themoviedb.org → Settings → API. Powers automatic metadata matching. |
| **Base URL** | Your public address, e.g. `https://your-domain.com`. **Important:** Stremio uses this to reach your streams. |
| **Upstream Repo / Branch** | Optional — used by `/restart` to auto-update (e.g. repo `weebzone/Telegram-Stremio`, branch `master`). |

### 💳 Subscription (optional)
Turn this on to monetise access. Set the **Subscription Group ID**, **Payment Instructions** (your UPI / bank / PayPal text), an optional **Payment QR image URL**, and the **Approver IDs** (who can approve requests). Renewal and "join the channel" prompts shown in Stremio point users back to **your bot automatically** — no separate URL to configure. The full flow is described in [Subscription Management](#-subscription-management).

### 🌐 Global Search (optional)
Requires `USER_SESSION_STRING` in `config.env` plus one app restart to unlock. Then enable the toggle and add the **channel IDs** to search. Results that aren’t in your local catalog are tagged **🌐 GLOBAL** in Stremio.

### 🌐 Proxy (optional)
Set an **HTTP Proxy URL** for outbound metadata/API requests, and optionally **show both** proxied and direct stream links.

### 🗄️ Extra Storage Databases
Your first two databases (from `config.env`) are **locked** as *Tracking* and *Storage 1*. Add more MongoDB URIs here to expand storage capacity — 🟢 means connected. Remove entries only from the **end** of the list, since existing media reference databases by position.

### 📨 Multi-Token Clients
Add extra **bot tokens** for faster parallel streaming under heavy load. Create more bots with @BotFather, add them as **admins** in all your AUTH channels, then paste their tokens here. Changes apply immediately.

> ✅ Click **Save Settings** when you’re done. That’s it — you’re live!

---

# 💳 Subscription Management

The Subscription Management system allows you to **monetise access** to your Telegram Stremio server. When enabled, users must have an active subscription to stream content.

## 📋 Subscription Plans

Admins can create and manage subscription plans from the **Admin Panel → Subscription Management** page.

Each plan has:
- **Name** (e.g. `Monthly`, `Quarterly`)
- **Duration** in days
- **Price** (for display)
- **Description**

Plans are stored in MongoDB and can be added, edited, or deleted at any time without restarting.

---

## 🤖 Bot Payment Flow

Users interact with the bot to subscribe:

```
User → /start → selects plan → sends payment screenshot
      → Approver gets notification → Approve / Reject
      → On Approve:
          ✅ Subscription saved to DB
          🔑 Stremio addon token auto-generated
          📨 User receives Stremio install link + group invite
```

**Approver actions** (available to `APPROVER_IDS`):

| Button | Action |
| :--- | :--- |
| ✅ Approve | Activates subscription, generates addon token, invites user to group |
| ❌ Reject | Notifies user with rejection message |

---

## 🗃️ Access Management

The **Admin Panel → Access Management** page gives admins full control over all users and their addon tokens.

### Columns Shown

| Column | Description |
| :--- | :--- |
| Status | 🟢 Active / 🔴 Expired |
| User | Display name or `User {id}` |
| Addon Link | Stremio install URL + copy button |
| Created | Token creation date |
| Expires | Subscription expiry date |
| Actions | Buttons for managing the user |

### Action Buttons

| Button | Description |
| :--- | :--- |
| 📅 **Assign** | Assign or extend a subscription plan (adds days) |
| ➕ **Extend** | Add extra days to an active subscription |
| ➖ **Reduce** | Subtract days from an active subscription |
| 🚫 **Revoke** | Wipe subscription entirely (marks expired) |
| 🗑️ **Del Token** | Delete the addon token only (user still subscribed) |
| 🔗 **Link User ID** | Link an old/orphan token to a Telegram user ID to enable management |

> 💡 Manually created (old) tokens that have no linked user ID show a **🔗 Link User ID** button. Once linked, all action buttons become available.

### Search & Filtering

- 🔍 Search by user name or ID
- Filter by status: All / Active / Expired
- Pagination with configurable page size

---

## 🎬 Stremio Addon Integration

### Per-User Addon Token

Each user gets a **unique addon token** automatically generated on payment approval. Their Stremio addon URL is:

```
https://your-domain.com/stremio/{token}/manifest.json
```

### Dynamic Manifest

The addon manifest updates dynamically per user:

| Scenario | Addon Name | Description |
| :--- | :--- | :--- |
| Active, has expiry | `Telegram — Expires 28 Mar 2026` | 📅 Subscription active until 28 Mar 2026 |
| Active, no expiry | `Telegram — Active` | ✅ Subscription active |
| Default (no subscription mode) | `Telegram` | Standard description |

The manifest `version` encodes the expiry date — when an admin extends or revokes a subscription, the version changes and Stremio detects an update.

### Subscription Stream Gating

When the subscription feature is enabled, the addon checks every stream request and shows a single actionable entry instead of the streams when the user isn't eligible. In both cases the stream link opens **your bot** (derived automatically from the bot's username — there is no URL to configure).

**Plan expired** — the user's subscription has lapsed:

```json
{
  "name": "🚫 Plan Expired",
  "title": "Your plan is expired.\nRenew it from the bot to continue watching.",
  "url": "https://t.me/your_bot"
}
```

**Not joined** — the user is active but has left / never joined the subscription channel (the `Subscription Group ID`):

```json
{
  "name": "📢 Join Required",
  "title": "First join the channel to stream it.\nTap here to open the bot and join.",
  "url": "https://t.me/your_bot"
}
```

Clicking the stream name opens the bot directly so the user can renew or rejoin. The membership check fails open — if Telegram is briefly unreachable or the bot can't read the group, legitimate users are never blocked.

### Configure & Reinstall Page

Every addon has a **Configure page** at:

```
https://your-domain.com/stremio/{token}/configure
```

This page shows:
- User name, subscription status, expiry date
- **⚡ Install / Update in Stremio** button (Stremio Web install flow)
- Manual install steps + **📋 Copy URL** button

The ⚙️ gear icon in Stremio opens this page so users can reinstall after an admin updates their subscription.

---

# 🚀 Deployment Guide

This guide will help you deploy your **Telegram Stremio Media Server** using either Heroku or a VPS with Docker.

## ✅ Recommended Prerequisites

**Supported Servers:**

  - 🟣 **Heroku**
  - 🟢 **VPS** 

Before you begin, ensure you have:

1.  ✅ A **VPS** with a public IP (e.g., Ubuntu on DigitalOcean, AWS, Vultr, etc.)
2.  ✅ A **Domain name**


## 🐙 Heroku Guide

Follow the instructions provided in the Google Colab Tool to deploy on Heroku.

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/weebzone/Colab-Tools/blob/main/telegram%20stremio.ipynb)


## 🐳 VPS Guide

This section explains how to deploy your **Telegram Stremio Media Server** on a VPS using **Docker Compose (recommended)** or **Docker**.


### 1️⃣ Step 1: Clone & Configure the Project

```bash
git clone https://github.com/weebzone/Telegram-Stremio
cd Telegram-Stremio
mv sample_config.env config.env
nano config.env
```

* Fill in all required variables in `config.env`.
* Press `Ctrl + O`, then `Enter`, then `Ctrl + X` to save and exit.

## ⚙️ Step 2: Choose Your Deployment Method

You can deploy the server using either **Docker Compose (recommended)** or **plain Docker**.



### 🟢 **Option 1: Deploy with Docker Compose (Recommended)**

Docker Compose provides an easier and more maintainable setup, environment mounting, and restart policies.

#### 🚀 Start the Container

```bash
docker compose up -d
```

Your server will now be running at:
➡️ `http://<your-vps-ip>:8000`

---

#### 🛠️ Update `config.env` While Running

If you need to modify environment values (like `BASE_URL`, `AUTH_CHANNEL`, etc.):

1. **Edit the file:**

   ```bash
   nano config.env
   ```
2. **Save your changes:** (`Ctrl + O`, `Enter`, `Ctrl + X`)
3. **Restart the container to apply updates:**

   ```bash
   docker compose restart
   ```

⚡ Since the config file is mounted, you **don’t need to rebuild** the image — changes apply automatically on restart.



### 🔵 **Option 2: Deploy with Docker (Manual Method)**

If you prefer not to use Docker Compose, you can manually build and run the container.

#### 🧩 Build the Image

```bash
docker build -t telegram-stremio .
```

#### 🚀 Run the Container

```bash
docker run -d -p 8000:8000 telegram-stremio
```

Your server should now be running at:
➡️ `http://<your-vps-ip>:8000`



### 🌐 Step 3: Add Domain (Required)

#### 🅰️ Set Up DNS Records

Go to your domain registrar and add an **A record** pointing to your VPS IP:

| Type | Name | Value             |
| ---- | ---- | ----------------- |
| A    | @    | `195.xxx.xxx.xxx` |


#### 🧱 Install Caddy (for HTTPS + Reverse Proxy)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

#### ⚙️ Configure Caddy

1. **Edit the Caddyfile:**

   ```bash
   sudo nano /etc/caddy/Caddyfile
   ```

2. **Replace contents with:**

   ```caddy
   your-domain.com {
       reverse_proxy localhost:8000
   }
   ```

   * Replace `your-domain.com` with your actual domain name.
   * Adjust the port if you changed it in `config.env`.

3. **Save and reload Caddy:**

   ```bash
   sudo systemctl reload caddy
   ```


✅ Your API will now be available securely at:
➡️ `https://your-domain.com`


## 🤗 Hugging Face Guide

Deploy a **free, always-online** instance — no VPS, no domain, and no Docker knowledge required. Hugging Face builds the Docker image **on its own servers**; you only tap a few buttons.

> 💡 **How it works:** this repo ships a GitHub Action that automatically pushes your code to your Hugging Face Space on every change. The Space then builds the included `Dockerfile` and runs your server.

### ⭐ Step 1: Star this Repository

Open the repo and tap **⭐ Star** at the top right. It helps the project and keeps it on your radar.

➡️ [github.com/weebzone/Telegram-Stremio](https://github.com/weebzone/Telegram-Stremio)

### 🍴 Step 2: Fork the Repository

Tap **Fork** (top right) → **Create fork**. This gives you your **own copy** so you can add private secrets and run the deploy workflow.

### 🔑 Step 3: Create a Hugging Face Write Token

1. Sign in (or sign up) at [huggingface.co](https://huggingface.co).
2. Go to **Profile → Settings → Access Tokens**.
3. Tap **Create new token**, choose the **Write** role, and copy the token.

### 🚀 Step 4: Create a Docker Space

1. Go to [huggingface.co/new-space](https://huggingface.co/new-space).
2. Give it a name, select **Docker** as the SDK (pick the **Blank** template).
3. Set visibility to **Public** (required so Stremio/Nuvio can reach your addon).
4. Tap **Create Space**.

Your Space ID is `<your-hf-username>/<your-space-name>` — note it down.

### 🔐 Step 5: Add Deploy Credentials to Your GitHub Fork

In **your forked repo** → **Settings → Secrets and variables → Actions**:

| Type         | Name          | Value                                  |
| ------------ | ------------- | -------------------------------------- |
| **Secret**   | `HF_TOKEN`    | the Write token from Step 3            |
| **Variable** | `HF_SPACE_ID` | `<your-hf-username>/<your-space-name>` |

> Add the secret under the **Secrets** tab and the variable under the **Variables** tab.

### 🤖 Step 6: Add Your Bot Secrets to the Space

On your **Hugging Face Space → Settings → Variables and secrets**, add the same values you'd normally put in `config.env`:

| Secret                | Required | Where to get it                        |
| --------------------- | -------- | -------------------------------------- |
| `API_ID`              | ✅        | [my.telegram.org](https://my.telegram.org) |
| `API_HASH`            | ✅        | [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN`           | ✅        | [@BotFather](https://t.me/BotFather)   |
| `OWNER_ID`            | ✅        | your numeric Telegram ID               |
| `DATABASE`            | ✅        | two comma-separated MongoDB URIs       |
| `USER_SESSION_STRING` | ⬜        | optional (Global Search)               |

> ℹ️ You don't need a `config.env` file on Hugging Face — these secrets are read directly as environment variables. The included `Dockerfile` already listens on the right port (`app_port: 8000` is preset in this README).

### ▶️ Step 7: Deploy

In **your forked repo** → **Actions** tab → select **Deploy to Hugging Face Space** → **Run workflow**.

After this first run, **every push to your fork auto-deploys**. Watch the build progress on your Hugging Face Space page — once it shows **Running**, you're live.

### 🎬 Step 8: Use Your Addon

1. Open `https://<your-hf-username>-<your-space-name>.hf.space/login`
2. Log in (default `admin` / `admin`) and **immediately change the password**.
3. Set your **Base URL** in the web Settings page to `https://<your-hf-username>-<your-space-name>.hf.space`.
4. Now open bot and send /start command it give you the manifest url
5. Add the manifest URL to Stremio/Nuvio and enjoy. 🎉

### 🎬 Step 9: Setup the Additional value

1. Go to Web settings page `https://<your-hf-username>-<your-space-name>.hf.space/admin/settings`.
2. Fill up the TMDB API, AUTH CHANNEL.
3. If you need to fill up other things check [Web Settings](#-step-4-configure-everything-else-web-settings-page)
4. Save the settings and enjoy.

# 📺 Setting Up Your App (Nuvio Recommended)

Your media server works as a standard **Stremio-style addon**, so it plays in any compatible client. For the **best compatibility and smoothest experience across devices, we recommend the [Nuvio](https://play.google.com/store/apps/details?id=com.nuvio.app) app** — a free, open-source media hub for **Android, Android TV, Fire TV, iOS, Windows, and TV** that supports Stremio addon manifest URLs natively. *(Content was rephrased for compliance with licensing restrictions.)*

> 💡 Already using **Stremio**? It works too — just install the same addon URL below. Nuvio simply tends to handle these Telegram streams more reliably across more devices.

## 📥 Step 1: Install Nuvio

Download Nuvio from an official source:

| Platform | Source |
| :--- | :--- |
| **Android / Android TV / Fire TV** | [Google Play](https://play.google.com/store/apps/details?id=com.nuvio.app) |
| **All platforms / latest builds** | [GitHub — tapframe/NuvioStreaming](https://github.com/tapframe/NuvioStreaming) |

> 🔗 *(Optional)* Connect **Trakt** in the app to sync your watch history and progress across devices.

## 🌐 Step 2: Add the Addon

1. Open **Nuvio** and go to the **Addons** section.
2. Paste your addon **manifest URL** and install it:

3. Done! 🎉 Your Telegram library now appears in the catalog and streams directly.



## 🏅 Contributors

|<img width="80" src="https://avatars.githubusercontent.com/u/113664541">|<img width="80" src="https://avatars.githubusercontent.com/u/13152917">|<img width="80" src="https://avatars.githubusercontent.com/u/14957082">|<img width="80" src="https://raw.githubusercontent.com/vflixa1prime/Readme/main/VFlixPRime.png">|
|:---:|:---:|:---:|:---:|
|[`Karan`](https://github.com/Weebzone)|[`Stremio`](https://github.com/Stremio)|[`ChatGPT`](https://github.com/OPENAI)|[`VFlix Prime`](https://t.me/vflixprime2)|
|Author|Stremio SDK|Refactor|Community Support
