
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

- [🚀 Introduction](#-introduction)
  - [✨ Key Features](#-key-features)
- [⚙️ How It Works](#️-how-it-works)
  - [Overview](#overview)
  - [Upload Guidelines](#upload-guidelines)
  - [Quality Replacement](#-quality-replacement-logic)
  - [Updating CAMRip](#-updating-camrip-or-low-quality-files)
  - [Behind The Scenes](#behind-the-scenes)
- [🤖 Bot Commands](#-bot-commands)
  - [Command List](#command-list)
  - [`/set` Command Usage](#set-command-usage)
- [🔧 Configuration Guide](#-configuration-guide)
  - [🧩 Startup Config](#-startup-config)
  - [🗄️ Storage](#️-storage)
  - [🎬 API](#-api)
  - [🌐 Server](#-server)
  - [🔄 Update Settings](#-update-settings)
  - [🔐 Admin Panel](#-admin-panel)
  - [🧰 Additional CDN Bots (Multi-Token System)](#-additional-cdn-bots-multi-token-system)
- [🚀 Deployment Guide](#-deployment-guide)
  - [✅ Recommended Prerequisites](#-recommended-prerequisites)
  - [🐙 Heroku Guide](#-heroku-guide)
  - [🐳 VPS Guide (Recommended)](#-vps-guide)
- [📺 Setting up Stremio](#-setting-up-stremio)
  - [🌐 Add the Addon](#-step-3-add-the-addon)
  - [⚙️ Optional: Remove Cinemeta](#️-optional-remove-cinemeta)
- [🏅 Contributor](#-contributor)


# 🚀 Introduction

This project is a **next-generation Telegram Stremio Media Server** that allows you to **stream your Telegram files directly through Stremio**, without any third-party dependencies or file expiration issues. It’s designed for **speed, scalability, and reliability**, making it ideal for both personal and community-based media hosting.


## ✨ Key Features

- ⚙️ **Multiple MongoDB Support** 
- 📡 **Multiple Channel Support** 
- ⚡ **Fast Streaming Experience**
- 🔑 **Multi Token Load Balancer** 
- 🎬 **IMDB and TMDB Metadata Integration** 
- ♾️ **No File Expiration** 
- 🧠 **Admin Panel Support** 


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

When you upload multiple files with the **same quality label** (like `720p` or `1080p`),
the **latest file automatically replaces the old one**.

> Example:
> If you already uploaded `Ghosted 2023 720p` and then upload another `720p` version,
> the bot **replaces the old file** to keep your catalog clean and organized.

This helps avoid duplicate entries in Stremio and ensures only the most recent file is used.

---

### 🆙 Updating CAMRip or Low-Quality Files

If you initially uploaded a **CAMRip or low-quality version**, you can easily replace it with a better one:

1. Forward the **new, higher-quality file** (e.g., `1080p`, `WEB-DL`) to your **AUTH CHANNEL**.
2. The bot will **automatically detect and replace** the old CAMRip file in the database.
3. The Stremio addon will then **update automatically**, showing the new stream source.

✅ No manual deletion or command is needed — forwarding the updated file is enough!

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

All environment variables for this project are defined in the `config.env` file. A detailed explanation of each parameter is provided below.

### 🧩 Startup Config

| Variable | Description |
| :--- | :--- |
| **`API_ID`** | Your Telegram **API ID** from [my.telegram.org](https://my.telegram.org). Used for authenticating your Telegram session. |
| **`API_HASH`** | Your Telegram **API Hash** from [my.telegram.org](https://my.telegram.org). |
| **`BOT_TOKEN`** | The main bot’s **access token** from [@BotFather](https://t.me/BotFather). Handles user requests and media fetching. |
| **`HELPER_BOT_TOKEN`** | **Secondary bot token** used to assist the main bot with tasks like deleting, editing, or managing. |
| **`OWNER_ID`** | Your **Telegram user ID**. This ID has full administrative access. |
| **`REPLACE_MODE`** | When `true`, new files replace existing files of the same quality. When `false`, multiple files of the same quality are allowed. |
| **`HIDE_CATALOG`** | When `true`, the default Telegram Stremio Catalog is hidden, and streams only show in the Cinemata catalog (i.e., Cinemata addon is mandatory). Default is `false`. |
| **`PARALLEL`** | Controls the number of parallel chunks/connections used during streaming. Higher values can improve download speed and reduce buffering but will increase CPU, memory usage, and Telegram API load. Default is `1` (for this you should have more Multi Tokens). |
| **`PRE_FETCH`** | Enables prefetching of upcoming stream chunks before they are requested by the player. Higher values allow smoother playback and faster seeking at the cost of extra bandwidth and memory usage. Default is `1` (for this you should have more Multi Tokens). |

### 🗄️ Storage

| Variable | Description |
| :--- | :--- |
| **`AUTH_CHANNEL`** | One or more **Telegram channel IDs** (comma-separated) where the bot is authorized to fetch or stream content. *Example: `-1001234567890, -1009876543210`*. |
| **`DATABASE`** | MongoDB Atlas connection URI(s). You **must provide at least two databases**, separated by commas (`,`) for load balancing and redundancy. <br>Example: <br>`mongodb+srv://user:pass@cluster0.mongodb.net/db1, mongodb+srv://user:pass@cluster1.mongodb.net/db2` |

> 💡 **Tip:** Create your MongoDB Atlas cluster [here](https://www.mongodb.com/cloud/atlas).

### 🎬 API

| Variable | Description |
| :--- | :--- |
| **`TMDB_API`** | Your **TMDB API key** from [themoviedb.org](https://www.themoviedb.org/settings/api). Used to fetch movie and TV metadata. |

### 🌐 Server

| Variable | Description |
| :--- | :--- |
| **`BASE_URL`** | The Domain or Heroku app URL (e.g. `https://your-domain.com`). Crucial for Stremio addon setup. |
| **`PORT`** | The port number on which your FastAPI server will run. *Default: `8000`*. |

### 🔄 Update Settings

| Variable | Description |
| :--- | :--- |
| **`UPSTREAM_REPO`** | GitHub repository URL for automatic updates. |
| **`UPSTREAM_BRANCH`** | The branch name to track in your upstream repo. *Default: `master`*. |

### 🔐 Admin Panel

| Variable | Description |
| :--- | :--- |
| **`ADMIN_USERNAME`** | Username for logging into the Admin Panel. |
| **`ADMIN_PASSWORD`** | Password for Admin Panel access.|
 **⚠️ Change from default values for security.** 

### 🧰 Additional CDN Bots (Multi-Token System)

| Variable | Description |
| :--- | :--- |
| **`MULTI_TOKEN1`**, **`MULTI_TOKEN2`**, ... | Extra bot tokens used to distribute traffic and prevent Telegram rate-limiting. Add each bot as an **Admin** in your `AUTH_CHANNEL`(s). |

#### About `MULTI_TOKEN`

If your bot handles a high number of downloads/requests at a time, Telegram may limit your main bot.  
To avoid this, you can use **MULTI_TOKEN** system:

- Create multiple bots using [@BotFather](https://t.me/BotFather).
- Add each bot as **Admin** in your `AUTH_CHANNEL`(s).
- Add the tokens in your `config.env` as `MULTI_TOKEN1`, `MULTI_TOKEN2`, `MULTI_TOKEN3`, and so on.
- The system will automatically distribute the load among all these bots!


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

|<img width="80" src="https://avatars.githubusercontent.com/u/113664541">|<img width="80" src="https://avatars.githubusercontent.com/u/13152917">|<img width="80" src="https://avatars.githubusercontent.com/u/14957082">|
|:---:|:---:|:---:|
|[`Karan`](https://github.com/Weebzone)|[`Stremio`](https://github.com/Stremio)|[`ChatGPT`](https://github.com/OPENAI)|
|Author|Stremio SDK|Refactor

