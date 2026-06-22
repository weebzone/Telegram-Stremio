
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

  * [🚀 Quick Setup](#-quick-setup)
  * [🧩 Startup Configuration](#-startup-configuration)
  * [📚 Media & Catalog Settings](#-media--catalog-settings)
  * [🌐 Proxy Settings](#-proxy-settings)
  * [💳 Subscription System](#-subscription-system)
  * [🧠 Admin Panel](#-admin-panel)
  * [🔄 Update Settings](#-update-settings)
  * [🗄️ Multi-Database Support](#️-multi-database-support)
  * [🔑 Multi-Token System](#-multi-token-system)
  * [🔍 Global Search Requirements](#-global-search-requirements)

* [🚀 Deployment Guide](#-deployment-guide)

  * [✅ Recommended Prerequisites](#-recommended-prerequisites)
  * [🐙 Heroku Guide](#-heroku-guide)
  * [🐳 VPS Guide (Recommended)](#-vps-guide-recommended)

* [📺 Setting Up Stremio](#-setting-up-stremio)

  * [🌐 Add the Addon](#-step-3-add-the-addon)
  * [⚙️ Optional: Remove Cinemeta](#️-optional-remove-cinemeta)

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
/set https://m.imdb.com/title/tt665723
```

**Steps:**

1.  Send the `/set` command followed by the **IMDB URL** of the movie or show you want to upload.
2.  **Forward the related movie or TV show files** to your channel.
3.  Once all files are uploaded, **clear the default IMDB link** by simply sending the `/set` command without any URL.

💡 **Tip:** Use `/log` if you encounter any upload or parsing issues.



# 🔧 Configuration Guide

> 💡 You only need to edit `config.env` for startup credentials, database connection, and optional Userbot configuration. Everything else can be managed from the Web UI.

---

## 🧩 Startup Configuration

| Variable                  | Description                                                                                                 |
| :------------------------ | :---------------------------------------------------------------------------------------------------------- |
| **`API_ID`**              | Telegram API ID obtained from `my.telegram.org`. Required for all Telegram client connections.              |
| **`API_HASH`**            | Telegram API Hash obtained from `my.telegram.org`. Required for Telegram authentication.                    |
| **`BOT_TOKEN`**           | Main bot token generated via `@BotFather`. Used for streaming, indexing, and user interactions.             |
| **`OWNER_ID`**            | Your Telegram User ID. Grants full administrative access to the system.                                     |
| **`DATABASE`**            | MongoDB connection URI(s). Used to store metadata, settings, catalogs, subscriptions, and application data. |
| **`PORT`**                | Port on which the FastAPI server runs. Default: `8000`.                                                     |
| **`USER_SESSION_STRING`** | Optional Userbot session string. Required only for Global Search functionality.                             |

---

# ⚙️ Web Panel Configuration

The following settings are automatically loaded from the database and can be managed through the Web Admin Panel.

## 📚 Media & Catalog Settings

| Variable                     | Description                                                                                                    |
| :--------------------------- | :------------------------------------------------------------------------------------------------------------- |
| **`REPLACE_MODE`**           | When enabled, newly uploaded files replace existing files with the same quality label (`720p`, `1080p`, etc.). |
| **`HIDE_CATALOG`**           | Hides the default Telegram Stremio catalog and shows streams only through Cinemata integration.                |
| **`AUTH_CHANNEL`**           | Telegram channel IDs used for media indexing and streaming. Multiple channels are supported.                   |
| **`TMDB_API`**               | TMDb API key used to fetch movie and TV show metadata automatically.                                           |
| **`GLOBAL_SEARCH`**          | Enables searching media across configured Global Search channels. Requires a valid `USER_SESSION_STRING`.      |
| **`GLOBAL_SEARCH_CHANNELS`** | List of channel IDs included in Global Search. Searches are restricted to these channels only.                 |

---

## 🌐 Proxy Settings

| Variable                            | Description                                                             |
| :---------------------------------- | :---------------------------------------------------------------------- |
| **`HTTP_PROXY_URL`**                | Optional HTTP proxy used for metadata requests and external API access. |
| **`SHOW_PROXY_AND_NON_PROXY_BOTH`** | Shows both proxied and direct stream links simultaneously.              |

---

## 💳 Subscription System

| Variable                    | Description                                                                |
| :-------------------------- | :------------------------------------------------------------------------- |
| **`SUBSCRIPTION`**          | Enables subscription-based access control for streams.                     |
| **`SUBSCRIPTION_GROUP_ID`** | Telegram group/channel where approved subscribers are invited.             |
| **`APPROVER_IDS`**          | Telegram user IDs allowed to approve or reject subscription requests.      |
| **`SUBSCRIPTION_URL`**      | Bot or payment URL displayed to users when their subscription has expired. |

---

## 🧠 Admin Panel

| Variable             | Description                                                                                      |
| :------------------- | :----------------------------------------------------------------------------------------------- |
| **`ADMIN_USERNAME`** | Username used to access the Web Admin Panel.                                                     |
| **`ADMIN_PASSWORD`** | Password used to access the Web Admin Panel. Change the default values immediately for security. |

---

## 🔄 Update Settings

| Variable              | Description                                   |
| :-------------------- | :-------------------------------------------- |
| **`UPSTREAM_REPO`**   | Git repository URL used by the update system. |
| **`UPSTREAM_BRANCH`** | Branch that the updater should track.         |

---

## 🗄️ Multi-Database Support

| Variable              | Description                                                                                                                             |
| :-------------------- | :-------------------------------------------------------------------------------------------------------------------------------------- |
| **`EXTRA_DATABASES`** | Additional MongoDB databases used for load balancing, scalability, and redundancy. Can be added or removed directly from the Web Panel. |

---

## 🔑 Multi-Token System

| Variable           | Description                                                                                |
| :----------------- | :----------------------------------------------------------------------------------------- |
| **`MULTI_TOKENS`** | Additional bot tokens used to distribute download traffic and reduce Telegram rate limits. |

### About Multi-Token Support

If your addon handles a large number of concurrent streams or downloads, Telegram may rate-limit a single bot.

To avoid this:

1. Create multiple bots using `@BotFather`.
2. Add each bot as an admin in all AUTH CHANNELS.
3. Add their tokens in the **Multi Token** section of the Web Panel.
4. The system automatically distributes traffic across all available bots.

---

## 🔍 Global Search Requirements

Global Search requires a valid Userbot session.

* Configure `USER_SESSION_STRING` in `config.env`.
* Enable **Global Search** from the Web Panel.
* Add one or more **Global Search Channels**.
* Searches will only be performed within the configured Global Search channels.

> ⚠️ If `USER_SESSION_STRING` is not configured, Global Search cannot be enabled.


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

### Expired Stream

When a user's subscription expires, instead of streams they see:

```json
{
  "name": "🚫 Subscription Expired",
  "title": "Your subscription has expired.\nRenew via the bot to continue watching.",
  "url": "https://t.me/your_bot"   ← SUBSCRIPTION_URL from config
}
```

Clicking the stream name opens the bot directly for renewal.

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


# 📺 Setting up Stremio

Follow these steps to connect your deployed addon to the **Stremio** app.

### 📥 Step 1: Download Stremio

Download Stremio for your device:
👉 [https://www.stremio.com/downloads](https://www.stremio.com/downloads)

### 👤 Step 2: Sign In

  - Create or log in to your **Stremio account**.

### 🌐 Step 3: Add the Addon

1.  Open the **Stremio App**.
2.  Go to the **Addon Section** (usually represented by a puzzle piece icon 🧩).
3.  In the search bar, paste the appropriate addon URL:

| Deployment Method | Addon URL |
| :--- | :--- |
| **Heroku** | `https://<your-heroku-app>.herokuapp.com/stremio/manifest.json` |
| **Custom Domain** | `https://<your-domain>/stremio/manifest.json` |


## ⚙️ Optional: Remove Cinemeta

If you want to use **only** your **Telegram Stremio Media Server addon** for metadata and streaming, follow this guide to remove the default `Cinemeta` addon.

### 1️⃣ Step 1: Uninstall Other Addons

1.  Go to the **Addon Section** in the Stremio App.
2.  **Uninstall all addons** except your Telegram Stremio Media Server.
3.  Attempt to remove **Cinemeta**. If Stremio prevents it, proceed to Step 2.

### 2️⃣ Step 2: Remove “Cinemeta” Protection

1.  Log in to your **Stremio account** using **Chrome or Chromium-based browser** :
    👉 [https://web.stremio.com/](https://web.stremio.com/)
2.  Once logged in, open your **browser console** (`Ctrl + Shift + J` on Windows/Linux or `Cmd + Option + J` on macOS).
3.  Copy and paste the code below into the console and press **Enter**:

<!-- end list -->

```js
(function() {

	const token = JSON.parse(localStorage.getItem("profile")).auth.key;

    const requestData = {
        type: "AddonCollectionGet",
        authKey: token,
        update: true
    };

    fetch('https://api.strem.io/api/addonCollectionGet', {
        method: 'POST',
        body: JSON.stringify(requestData)
    })
    .then(response => response.json())
    .then(data => {

    if (data && data.result) {

        let result = JSON.stringify(data.result).substring(1).replace(/"protected":true/g, '"protected":false').replace('"idPrefixes":["tmdb:"]', '"idPrefixes":["tmdb:","tt"]');
            
        const index = result.indexOf("}}],");
            
        if (index !== -1) {
            result = result.substring(0, index + 3) + "}";
        }

		let addons = '{"type":"AddonCollectionSet","authKey":"' + token + '",' + result;

		fetch('https://api.strem.io/api/addonCollectionSet', {
    		method: 'POST',
			body: addons 
		})
      	.then(response => response.text())
      	.then(data => {
      		console.log('Success:', data);
      	})
      	.catch((error) => {
      		console.error('Error:', error);
      	});

        } else {
            console.error('Error:', error);
        }
    })
    .catch((error) => {
        console.error('Erro:', error);
    });
})();
```

### 3️⃣ Step 3: Confirm Success

  - Wait until you see this message in the console:
    ```
    Success: {"result":{"success":true}}
    ```
  - Refresh the page (**F5**). You will now be able to **remove Cinemeta** from your addons list.


## 🏅 **Contributor**

|<img width="80" src="https://avatars.githubusercontent.com/u/113664541">|<img width="80" src="https://avatars.githubusercontent.com/u/13152917">|<img width="80" src="https://avatars.githubusercontent.com/u/14957082">|<img width="80" src="https://raw.githubusercontent.com/vflixa1prime/Readme/main/VFlixPRime.png">|
|:---:|:---:|:---:|:---:|
|[`Karan`](https://github.com/Weebzone)|[`Stremio`](https://github.com/Stremio)|[`ChatGPT`](https://github.com/OPENAI)|[`VFlix Prime`](https://t.me/vflixprime2)|
|Author|Stremio SDK|Refactor|Community Support
