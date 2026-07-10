# Getting Started

This page takes you from **nothing** to **your first working stream**. Follow it in
order and you'll be watching in about 15–20 minutes.

```{admonition} Beginner Tip
:class: tip
You only need to fill in **6 values once** in a file called `config.env`. Everything
else is configured later from a friendly web page — no code, no restarts.
```

## What you need before starting

You'll need a few free accounts and tools. Grab these first so you don't get stuck
halfway:

Telegram account
: A normal Telegram account (the one on your phone is fine).

A Telegram bot
: Created for free through **@BotFather** (we show you how below).

Two MongoDB databases
: Free forever on **MongoDB Atlas** (the "M0" tier). This is where your library info lives.

A place to run the app
: A VPS, Hugging Face Space, Heroku, or even your own PC. See {doc}`deployment`.

A Stremio-compatible app
: **Stremio** or **Nuvio** on your phone, TV, or computer.

```{admonition} Good to Know
:class: note
A **TMDB API key** (free) is optional but strongly recommended — it's what fetches
posters, descriptions, and organizes your automatic catalogs.
```

## Prerequisites (the technical bits)

If you deploy with **Docker** (recommended), you don't need to install Python or
anything else — Docker handles it. If you run it directly on your machine, you'll need:

- **Python 3.10+**
- **MongoDB** connection strings (from Atlas)
- The Python packages listed in `requirements.txt`

## Step 1 — Get your Telegram credentials

1. **API ID & API HASH** — Go to [my.telegram.org](https://my.telegram.org), log in,
   open **API development tools**, and create an app (any title works). Copy the
   **api_id** and **api_hash**.
2. **Bot Token** — Open **@BotFather** in Telegram, send `/newbot`, follow the prompts,
   and copy the token it gives you.
3. **Owner ID** — Open **@userinfobot** in Telegram; it replies with your numeric ID.

```{admonition} Common Mistake
:class: warning
After creating your bot, you **must add it as an admin** in every channel you'll use
for media. If you forget this, no files will be indexed.
```

## Step 2 — Get your MongoDB databases

1. Create a free account at [MongoDB Atlas](https://www.mongodb.com/atlas) and create a cluster.
2. Under **Database Access**, add a username and password.
3. Under **Network Access**, add `0.0.0.0/0` (allow from anywhere).
4. Click **Connect → Drivers** and copy the connection string. It looks like:
   `mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/`
5. You need **two** — add a database name to the end of each, e.g. `/tracking` and `/storage1`.

```{admonition} Beginner Tip
:class: tip
You can use the **same cluster** for both databases — just give them two different
names at the end of the URL. The first is for tracking/metadata, the second stores
your media references.
```

## Step 3 — Create your `config.env` file

Clone the project and copy the sample config:

```bash
git clone https://github.com/weebzone/Telegram-Stremio
cd Telegram-Stremio
cp sample_config.env config.env
```

Open `config.env` and fill in these values:

```{list-table}
:header-rows: 1
:widths: 25 15 60

* - Variable
  - Required
  - What it is
* - `API_ID`
  - ✅
  - Telegram API ID (from my.telegram.org)
* - `API_HASH`
  - ✅
  - Telegram API Hash (from my.telegram.org)
* - `BOT_TOKEN`
  - ✅
  - Your bot token (from @BotFather)
* - `OWNER_ID`
  - ✅
  - Your numeric Telegram user ID
* - `DATABASE`
  - ✅
  - **Two** MongoDB URIs, separated by a comma
* - `PORT`
  - ✅
  - Web server port (keep `8000` unless it's busy)
* - `USER_SESSION_STRING`
  - ⬜
  - Optional — only needed for Global Search
```

A finished file looks like this (example values):

```ini
API_ID="1234567"
API_HASH="abc123def456ghi789jkl012mno345pq"
BOT_TOKEN="1234567890:AAEabcdEFGhijkLMnOPqrsTUVwxyz12345"
USER_SESSION_STRING=""
OWNER_ID="987654321"
DATABASE="mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/tracking,mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/storage1"
PORT="8000"
```

```{admonition} Common Mistake
:class: warning
The `DATABASE` value must contain **exactly two** connection strings separated by a
single comma, with **no spaces** around the comma.
```

## Step 4 — Run the app

The easiest way is Docker Compose:

```bash
docker compose up -d
```

Your server is now running at `http://<your-server-ip>:8000`.

```{admonition} Good to Know
:class: note
Prefer a free, always-online option with no server to manage? See the
**Hugging Face** guide in {doc}`deployment`.
```

## Step 5 — Finish setup in the web panel

1. Open `http://<your-server-ip>:8000/login` in your browser.
2. Log in with the default credentials — **`admin` / `admin`**.
3. **Change the admin password immediately** (Settings → Admin Authentication).
4. Fill in your **TMDB API Key**, **Base URL**, and **AUTH_CHANNEL(s)**, then **Save Settings**.

See {doc}`configuration` for what every field does.

## Step 6 — Add your first file and watch it

1. Add your bot as an **admin** to your Telegram channel.
2. Make sure that channel is listed in **AUTH_CHANNEL** in the web settings.
3. Forward a movie with a good caption, e.g. `Ghosted 2023 720p WEBRip.mkv`.
4. In Telegram, send `/start` to your bot — it replies with your **addon URL**.
5. Paste that addon URL into Stremio/Nuvio.
6. Your movie now appears in the catalog. Press play. 🎉

## Verify everything is working

Use this quick checklist:

- [ ] The web panel loads at your URL and you can log in.
- [ ] Settings shows a green/connected status for your databases.
- [ ] Your bot replies to `/start` with an addon URL.
- [ ] A forwarded file appears under **Media Management** in the panel.
- [ ] The title shows a poster and description (means TMDB/IMDb matching works).
- [ ] The addon appears in Stremio and the file plays.

```{admonition} Stuck?
:class: important
If a file doesn't show up, the caption is almost always the reason. Jump to
{doc}`media-management` to learn the exact naming rules, or {doc}`troubleshooting`
for common fixes.
```
