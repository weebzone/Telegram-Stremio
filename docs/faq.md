# FAQ

Quick answers to the questions people ask most.

**Why are there multiple databases?**
: The first database (`tracking`) stores settings, tokens, and catalogs. The others
  (`storage_1`, `storage_2`, …) store your actual media entries. Splitting them lets you
  grow a big library across several **free** MongoDB tiers without paying. See {doc}`configuration`.

**Why Telegram as storage?**
: Telegram offers generous, reliable file hosting with no expiring links. This app turns
  that storage into a streamable, organized library — no re-uploading required.

**Can I use private channels?**
: Yes. Add the bot as an **admin** to your private channel and list it in AUTH_CHANNEL. For
  manual entries and Global Search, private links (`t.me/c/...`) are supported too.

**Can I stream lectures or personal videos?**
: Absolutely. Use **Manual Add** for content that has no IMDb/TMDB page — you supply the
  title and details. See {doc}`media-management`.

**Can multiple users stream at the same time?**
: Yes. For heavy simultaneous use, add extra **bot tokens** (multi-token load balancing) so
  streams are spread across several bots.

**Can I use Cloudflare / a reverse proxy?**
: Yes. Run the app behind Caddy, Nginx, or Cloudflare for HTTPS. Just set your **Base URL**
  to the public HTTPS address. See {doc}`deployment`.

**Do I have to pay for anything?**
: No. You can run entirely on free tiers (Hugging Face + free MongoDB). A VPS and domain are
  optional upgrades for performance and a clean URL.

**Do I need a TMDB key?**
: It's optional but strongly recommended. Without it, posters/descriptions are limited and
  language/OTT auto-catalogs won't populate (only *Top Rated* and *Recently Added* will).

**How much RAM do I need?**
: The app is lightweight. A small VPS (about **1 GB RAM**) is plenty for personal use. Heavy
  multi-user streaming benefits from more, but streaming is bandwidth-bound, not RAM-bound.

**Will my links expire?**
: No. The app stores the channel + message reference and reconnects to Telegram on demand,
  so links keep working indefinitely.

**How do I charge people for access?**
: Enable **Subscription** mode, create plans, and set approvers. Users pay through the bot
  and get an auto-generated addon token on approval. See {doc}`user-management`.

**Which app should I watch in — Stremio or Nuvio?**
: Both work. **Nuvio** tends to handle these Telegram streams more reliably across more
  devices, so it's the recommended client.
