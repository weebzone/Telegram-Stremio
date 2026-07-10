# Search

Your library is searchable from inside Stremio, and you can optionally search **beyond**
your local catalog with Global Search.

## Local search

By default, Stremio's search box queries **your indexed library** — the movies and shows
already stored in your databases. Results are filtered and sorted for you, respecting each
title's visibility settings.

## Global Search (optional)

Global Search lets the app look through **other Telegram channels you belong to** — not
just your AUTH channels — and surface matching files on the fly.

### Why it needs a session string

A bot can only see channels it's a member of. To reach your *personal* channels, the app
briefly uses **your own account session** (the `USER_SESSION_STRING`). This is the same as
being logged into Telegram Web.

### Enabling it

1. Generate a `USER_SESSION_STRING` (see the guide in {doc}`getting-started` /
   {doc}`deployment`) and put it in `config.env`.
2. **Restart** the app once to load it.
3. In the web settings, enable **Global Search** and add the **channel IDs** to search.

```{admonition} Results are tagged
:class: note
Titles found through Global Search that aren't in your local catalog are marked with a
**🌐 GLOBAL** tag in Stremio, so you can tell them apart from your own library.
```

```{admonition} Keep your session string private
:class: warning
Anyone with your `USER_SESSION_STRING` can access your Telegram account. Never share it or
commit it to a public repo. You can revoke it anytime from **Telegram → Settings → Devices**.
```
