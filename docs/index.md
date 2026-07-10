# Telegram-Stremio

Welcome to the **Telegram-Stremio** documentation — a self-hosted media server that
turns your Telegram channels into a personal Netflix-style library you can watch
inside **Stremio** (or **Nuvio**) on any device.

```{admonition} What is this, in one sentence?
:class: tip
You upload movies and shows to a private Telegram channel, and this app makes them
appear as a browsable, streamable catalog in your Stremio-compatible app — with
posters, descriptions, seasons, and episodes filled in automatically.
```

## Who is this for?

- **Beginners** who have never written code and just want their own streaming library.
- **Power users** who want subscriptions, multiple databases, and load balancing.
- **Developers** who want to understand the API and internals.

You do **not** need to be a programmer. If you can copy, paste, and click buttons,
you can run this.

## What can it do?

- ⚡ Stream movies and TV shows straight from Telegram — no re-uploading, no file limits.
- ♾️ Permanent streaming links that never expire.
- 🎬 Automatic posters, descriptions, cast, and ratings from IMDb / TMDB.
- 🧩 Play split files (`.001`, `.002`) and multi-part videos as one smooth stream.
- 📚 Automatic catalogs (Bollywood, Anime, Netflix, Top Rated, and more).
- 🖥️ A friendly web admin panel — no command line needed for daily use.
- 💳 Optional paid subscriptions with an approval flow.
- 🔐 Per-user access tokens and permissions.
- 🔍 Global search across your channels.

## How the pieces fit together

```text
   You upload a file            The app reads it            You watch it
┌─────────────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│  Telegram Channel    │ ──▶ │  Bot + FastAPI +     │ ──▶ │  Stremio / Nuvio │
│  (your movies/shows) │     │  MongoDB + PyroFork  │     │  on any device   │
└─────────────────────┘     └─────────────────────┘     └──────────────────┘
```

```{admonition} Beginner Tip
:class: tip
Think of Telegram as your **hard drive**, this app as the **librarian** that
organizes everything and prints nice labels, and Stremio as the **TV remote**
you use to actually watch.
```

## New here? Start with these

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} 🚀 Getting Started
:link: getting-started
:link-type: doc
Install the app and get your first stream working.
:::

:::{grid-item-card} ⚙️ Configuration
:link: configuration
:link-type: doc
Understand every setting in plain English.
:::

:::{grid-item-card} 🗂 Media Management
:link: media-management
:link-type: doc
How files are named, scanned, and organized.
:::

:::{grid-item-card} 🛠 Troubleshooting
:link: troubleshooting
:link-type: doc
Fix the most common problems fast.
:::
::::

```{toctree}
:hidden:
:caption: Get Started

getting-started
configuration
```

```{toctree}
:hidden:
:caption: Using the App

streaming
media-management
catalogs
search
user-management
admin-panel
themes
```

```{toctree}
:hidden:
:caption: Reference & Operations

api-reference
deployment
updating
```

```{toctree}
:hidden:
:caption: Help

troubleshooting
faq
tips
```

```{toctree}
:hidden:
:caption: Project

contributing
changelog
credits
license
support
```
