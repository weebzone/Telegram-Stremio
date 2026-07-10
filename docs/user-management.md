# User Management

This page covers **who can access your streams** — access tokens, permissions, and the
optional paid subscription system.

## The big picture

Every viewer accesses your addon through a **unique token** embedded in their addon URL:

```text
https://your-domain.com/stremio/{token}/manifest.json
```

That token identifies them, controls what they can see, and (if enabled) ties them to a
subscription. If you don't enable subscriptions, tokens simply act as access keys.

## Telegram users

Users interact with your **bot**. When they send `/start`, the bot responds based on your
setup:

- **No subscription mode** → they get an addon URL right away.
- **Subscription mode** → they're guided to choose a plan and pay first.

## Access tokens

From **Admin Panel → Access Management**, you manage every token:

```{list-table}
:header-rows: 1
:widths: 25 75

* - Column
  - Meaning
* - Status
  - 🟢 Active / 🔴 Expired
* - User
  - Display name or `User {id}`
* - Addon Link
  - Their Stremio install URL (with a copy button)
* - Created
  - When the token was made
* - Expires
  - Subscription expiry date
* - Actions
  - Buttons to manage the user
```

### Action buttons

```{list-table}
:header-rows: 1
:widths: 25 75

* - Button
  - What it does
* - 📅 **Assign**
  - Assign or extend a subscription plan (adds days)
* - ➕ **Extend**
  - Add extra days to an active subscription
* - ➖ **Reduce**
  - Subtract days
* - 🚫 **Revoke**
  - Wipe the subscription (marks expired)
* - 🗑️ **Del Token**
  - Delete only the addon token (user stays subscribed)
* - 🔗 **Link User ID**
  - Link an old/orphan token to a Telegram user ID so it can be managed
```

```{admonition} Beginner Tip
:class: tip
Old, manually created tokens that have no linked user show a **🔗 Link User ID** button.
Link it once and all the other management buttons unlock.
```

## Permissions & limits

Tokens can carry **usage limits** (for example daily or monthly bandwidth caps). The app
tracks how much each token streams and can enforce those limits, which is handy for paid
tiers or fair-use control.

## Subscriptions (optional)

Turn this on to **monetize access**. When enabled, users need an active subscription to
stream.

### Plans

Create plans in **Admin Panel → Subscription Management**. Each plan has a **name**,
**duration in days**, **price** (for display), and **description**. Plans can be added,
edited, or deleted anytime without restarting.

### The payment flow

```text
User → /start → picks a plan → sends payment screenshot
     → Approver is notified → Approve / Reject
     → On Approve:
         ✅ Subscription saved
         🔑 Stremio token auto-generated
         📨 User gets their install link + group invite
```

Approvers (the **Approver IDs** you configured) get **Approve** / **Reject** buttons in
Telegram.

### What the user sees in Stremio

The addon adapts per user:

- **Active, with expiry** → addon named like `Telegram — Expires 28 Mar 2026`.
- **Active, no expiry** → `Telegram — Active`.
- **Expired** → instead of streams, a single **🚫 Plan Expired** entry that opens your bot
  to renew.
- **Not joined** → a **📢 Join Required** entry that opens your bot to rejoin the group.

```{admonition} Fail-safe by design
:class: note
The "did they join the channel?" check **fails open** — if Telegram is briefly unreachable,
legitimate users are never wrongly blocked.
```

### Configure / reinstall page

Each user has a page at `/stremio/{token}/configure` showing their status, expiry, and an
**Install / Update in Stremio** button. The ⚙️ gear icon in Stremio opens this so users can
reinstall after you extend or change their subscription.
