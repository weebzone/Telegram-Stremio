# Deployment

You can run Telegram-Stremio several ways. Pick the one that matches your comfort level
and budget.

```{list-table}
:header-rows: 1
:widths: 25 20 55

* - Method
  - Cost
  - Best for
* - Hugging Face
  - Free
  - Beginners who want always-online with no server to manage
* - VPS + Docker
  - Paid VPS
  - Best performance and full control (recommended for real use)
* - Heroku
  - Varies
  - Quick guided deploy via a Colab notebook
* - Local Docker
  - Free
  - Testing on your own PC
```

## Docker (single container)

Build and run the included `Dockerfile`:

```bash
docker build -t telegram-stremio .
docker run -d -p 8000:8000 telegram-stremio
```

Your server runs at `http://<your-server-ip>:8000`.

## Docker Compose (recommended)

Compose is easier to maintain — it mounts your config and restarts automatically.

```bash
git clone https://github.com/weebzone/Telegram-Stremio
cd Telegram-Stremio
cp sample_config.env config.env
nano config.env      # fill in your 6 values
docker compose up -d
```

Because `config.env` is mounted, editing it and running `docker compose restart` applies
changes **without rebuilding** the image.

```{admonition} Beginner Tip
:class: tip
Compose also caps log size (so logs don't fill your disk) and restarts the app if it ever
crashes. It's the least-effort way to keep things running.
```

## VPS with a domain (HTTPS)

For real use, run it on a VPS behind a reverse proxy so you get a clean HTTPS domain.

1. **Point DNS** — add an `A` record for your domain to your VPS IP.
2. **Install Caddy** (it handles HTTPS certificates automatically).
3. **Configure Caddy** with a tiny config:

   ```text
   your-domain.com {
       reverse_proxy localhost:8000
   }
   ```

4. Reload Caddy. Your app is now at `https://your-domain.com`.

```{admonition} Why a reverse proxy?
:class: note
Stremio prefers **HTTPS**. A reverse proxy like Caddy (or Nginx/Cloudflare) gives you a
secure `https://` address and forwards traffic to the app on port 8000.
```

```{admonition} Set your Base URL!
:class: warning
After adding a domain, set **Base URL** in the web settings to that exact `https://` address.
Streams break if Base URL doesn't match how clients reach you.
```

## Hugging Face Spaces (free, always-online)

This is the easiest zero-cost option — Hugging Face builds the Docker image on its servers.

1. **Fork** the GitHub repo (so you can add secrets and run the workflow).
2. Create a **Hugging Face write token** (Profile → Settings → Access Tokens).
3. Create a **Docker Space** (Blank template), visibility **Public**.
4. In your GitHub fork → **Settings → Secrets and variables → Actions**, add:
   - Secret `HF_TOKEN` = your write token
   - Variable `HF_SPACE_ID` = `<your-hf-username>/<your-space-name>`
5. On the **Hugging Face Space → Settings → Variables and secrets**, add the same values
   you'd put in `config.env` (`API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `DATABASE`,
   and optionally `USER_SESSION_STRING`).
6. In your fork → **Actions → Deploy to Hugging Face Space → Run workflow**.

After the first run, **every push auto-deploys**. Your app lives at
`https://<user>-<space>.hf.space`. Set that as your **Base URL** in settings.

```{admonition} Good to Know
:class: note
On Hugging Face you don't need a `config.env` file — the Space secrets are read directly as
environment variables.
```

## Heroku

Follow the guided **Google Colab** notebook linked from the project README. It walks you
through a Heroku deploy step by step.

## Watching on your device (Nuvio / Stremio)

Your server works as a standard Stremio-style addon, so it plays in any compatible client.
**Nuvio** (free, open-source, available for Android, Android TV, Fire TV, iOS, Windows, and
TV) tends to handle these Telegram streams especially reliably. Regular **Stremio** works
too. *(Content was rephrased for compliance with licensing restrictions.)*

To connect:

1. Install [Nuvio](https://play.google.com/store/apps/details?id=com.nuvio.app) (or Stremio).
2. Send `/start` to your bot to get your **manifest URL**.
3. In the app's **Addons** section, paste the manifest URL and install.
4. Your Telegram library now appears and streams directly. 🎉
