# SlashDat

A Discord bot that places food delivery orders through the Yonder API.

## What's here

- `bot.py` — Discord bot with six slash commands:
  - `/ping` — latency check
  - `/yonder-health` — pings the Yonder API
  - `/yonder-status` — your Yonder account + daily plan
  - `/price` — prices a Wonder cart (no money moves)
  - `/order` — **the real deal**: prices → charges the customer via Stripe → places the order → DMs tracking
  - `/myorders` — your 10 most recent orders (state + tracking links)
- `yonder/` — reusable async Yonder API client
  - Retries only when the API says `retryable: true`, with exponential backoff + jitter, honouring `Retry-After`
  - Never auto-retries a confirm with `outcome_known: false` — raises `OutcomeUnknownError` for human handling
  - Full card numbers are never logged
- `payments/` — Stripe wrapper (Checkout Sessions + refunds)
- `order_flow.py` — the paid /order pipeline as a state machine, with DB persistence and crash resume
- `storage.py` — SQLite persistence (via aiosqlite)
- `.env.example` — template for your secrets
- `SLASHDAT_HANDOFF.md` — project context for future sessions

## Persistence & crash recovery

Every order is written to a local SQLite file (`slashdat.db` by default — change via `DATABASE_PATH`). The file is gitignored.

On every state change (`priced → paid → placed`/`refunded`/`needs_check`/`expired`), the row is updated. On startup, the bot loads every order still in a live state (`priced` or `paid`) and spawns its background pipeline again — so even if the bot crashes while a customer is paying, the order will be picked back up automatically.

**What this guarantees:**
- A customer who paid while the bot was down still gets their order placed as soon as it reboots.
- A customer who hadn't paid yet gets a working pay link again when the Stripe session is still open; if the 10-minute window has already elapsed the bot cancels cleanly on resume.
- If the Yonder 5-minute hold expired during downtime, reprice fails cleanly and the payment is refunded automatically — never a double-order.

**What this does NOT guarantee:**
- Exactly-once DM delivery. If the bot crashes between a terminal state transition and sending the user's DM, the DB will be correct but the user won't be notified. `/myorders` lets them check.
- Protection against bot being down for more than Stripe's session lifetime (24h by default). If someone pays after that, Stripe will reject the attempt itself.

## How `/order` behaves

```
customer runs /order ABC123 "742 Main St" 3.00
        │
        ▼
┌─────────────────────────────┐
│ Yonder POST /v1/checkouts   │  ← price the cart
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Stripe create Checkout Sess │  ← amount = yonder total × (1 + markup%)
└──────────────┬──────────────┘
               ▼
    bot DMs customer the pay link (ephemeral)
               │
       ┌───────┴────────┐
       ▼                ▼
  customer pays     10 min timeout
       │                │
       ▼                ▼
  reprice +         expire Stripe session,
  Yonder /confirm   cancel Yonder checkout
       │
   ┌───┴────────────────────────┬────────────────────┐
   ▼                            ▼                    ▼
 placed                 known-outcome fail      outcome unknown
 (DM tracking)          (refund + explain)      (NO refund,
                                                 DM owner for check)
```

Everything the customer sees is ephemeral or a DM. Nothing leaks into channel history.

## First-time setup (Windows)

1. **Install Python 3.11 or newer**: https://www.python.org/downloads/
   - On the installer, tick **"Add Python to PATH"**.

2. **Open PowerShell in this folder** and create a virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   If `Activate.ps1` is blocked, run this once as admin:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

3. **Create a Stripe account and get your test key**:
   - https://dashboard.stripe.com/register
   - Make sure "Test mode" (top right of the dashboard) is **on** while you develop.
   - **Developers → API keys** → copy the **Secret key** (`sk_test_...`).
   - You never need a Stripe webhook for the base version — the bot polls Stripe for payment status.

4. **Create a Discord bot**:
   - https://discord.com/developers/applications → **New Application** → name it SlashDat.
   - Left sidebar → **Bot** → **Reset Token** → copy the token.
   - Left sidebar → **OAuth2 → URL Generator**: tick scopes `bot` and `applications.commands`, then paste the generated URL in your browser to invite the bot to your test server.

5. **Fill in your secrets**:
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```
   Paste your Discord token, your Yonder API key (`yd_live_...`), and your Stripe key (`sk_test_...`).
   Put your test server's ID in `DISCORD_GUILD_IDS` so commands appear instantly.
   Put your own Discord user id in `OWNER_DISCORD_ID` so the bot can DM you if an order needs manual checking.

6. **Run the bot**:
   ```powershell
   python bot.py
   ```
   You should see `synced commands to guild ...` and your bot going online in Discord.

## Using the Yonder client from Python

```python
import asyncio
from yonder import YonderClient, OutcomeUnknownError, YonderAPIError

async def main():
    async with YonderClient() as yc:
        checkout = await yc.create_checkout(
            cart_code="ABC123",
            address="742 Example Ave, Springfield, IL",
            tip=3.00,
        )
        print("Total:", checkout["price"]["total"])

        # Bump the tip before placing.
        checkout = await yc.patch_checkout(checkout["id"], tip=5.00)

        try:
            placed = await yc.confirm_checkout(checkout["id"])
            print("Tracking:", placed["tracking"])
        except OutcomeUnknownError as e:
            # DO NOT RETRY. The order may already be on its way.
            # Surface this to a human.
            print("Needs human check:", e)
        except YonderAPIError as e:
            print("Yonder refused:", e.human_message(), "(code:", e.code + ")")

asyncio.run(main())
```

## Testing `/order` safely

While in Stripe test mode:
- Use card number `4242 4242 4242 4242`, any future expiry, any 3-digit CVC, any zip.
- Yonder itself still places a **real** order on Wonder, so run `/price` first until you're sure the flow works, and only `/order` with a cart you actually want to receive.

## Deploying to Railway (24/7 hosting)

Railway runs your bot in the cloud so it stays online when your laptop isn't.
It bills roughly a dollar or two a month for a small bot like this at idle.

### 1. Push the project to GitHub

If you don't have git:
- Install Git for Windows: https://git-scm.com/download/win (defaults are fine)
- Create a free GitHub account: https://github.com/signup

Then in PowerShell, from this project folder:

```powershell
git init
git add .
git commit -m "initial slashdat bot"
```

Then make a new empty repository on GitHub (private is fine — easier) and copy
the two commands GitHub shows under "…or push an existing repository". They
look like:

```powershell
git remote add origin https://github.com/your-username/slashdat.git
git branch -M main
git push -u origin main
```

**Before you push, open `.gitignore` and double-check** that `.env` and `*.db`
are listed. If you ever accidentally commit `.env`, rotate every key in it
immediately — the Yonder one via `POST /v1/api-keys/regenerate`, the Stripe
one in the Stripe dashboard, and the Discord one via "Reset Token" in the
Developer Portal.

### 2. Create the Railway project

- Sign up / log in: https://railway.com (they still answer at railway.app).
- Click **New Project → Deploy from GitHub repo**.
- Grant Railway access to your SlashDat repo. Pick it.
- Railway starts a build. The first one will fail because env vars aren't set
  yet — that's fine, we fix it next.

### 3. Set the environment variables

In your service → **Variables** tab, add every row from `.env.example` with
your real values:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | your production Discord bot token |
| `YONDER_API_KEY` | `yd_live_...` |
| `STRIPE_SECRET_KEY` | `sk_live_...` once you're ready for real money, `sk_test_...` to start |
| `STRIPE_SUCCESS_URL` | any https URL — `https://discord.com` is fine |
| `STRIPE_CANCEL_URL` | same |
| `STRIPE_MARKUP_PERCENT` | `0` or your markup, e.g. `15` |
| `OWNER_DISCORD_ID` | your Discord user id |
| `DISCORD_GUILD_IDS` | leave blank for global commands in production |
| `DATABASE_PATH` | **`/data/slashdat.db`** — this points at the volume we add next |

Leave `.env` out of your repo — Railway reads these from its own dashboard.

### 4. Add the persistent volume (so SQLite survives redeploys)

In your service → **Settings → Volumes → + New Volume**:

- **Mount path:** `/data`
- **Name:** whatever (`slashdat-db` is fine)
- **Size:** 1 GB is plenty for years of orders

Railway will restart the service after attaching the volume. Because you set
`DATABASE_PATH=/data/slashdat.db`, the bot will create and persist the DB
inside that volume. Redeploys do not wipe it.

### 5. Confirm it's running as a worker (no public URL)

A Discord bot doesn't accept inbound HTTP, so there's **no need to generate a
Railway domain**. In **Settings → Networking**, leave "Public Networking"
off. The `Procfile` in this repo (`worker: python bot.py`) tells Railway this
is a background worker, not a web service.

### 6. Watch the logs

Open the **Deployments** tab → latest deploy → **View logs**. You should see:

```
INFO slashdat: storage: db at /data/slashdat.db ready
INFO slashdat: synced commands globally (may take up to 1 hour)
INFO slashdat: logged in as SlashDat#1234
```

If you see `resumed N in-flight order(s) from the database`, that's the DB
replay doing its job after a redeploy.

Try `/ping` from your Discord server to confirm.

### 7. When you flip to real money

- Swap `STRIPE_SECRET_KEY` for your `sk_live_...` key (Stripe dashboard, top
  right, flip off "Test mode" first).
- Keep `OWNER_DISCORD_ID` set — the "needs human check" DMs are how you catch
  outcome-unknown orders before they turn into customer complaints.
- Set `STRIPE_MARKUP_PERCENT` to the margin you want to make.

### 8. Rolling the bot forward

Each time you commit and push to GitHub, Railway rebuilds and redeploys
automatically. Your SQLite DB is on the mounted volume, so **it survives
every deploy**. In-flight orders will be picked back up by `resume_pending()`
the moment the new build boots.

## What's next (not yet built)

- **Stripe webhooks.** Replace polling with signed webhooks once the bot is on a public URL. Fewer round trips, faster placement.
- **FORTER_DECLINE handling.** The spec allows one card swap before giving up on an account — not yet wired into `/order`.
- **Address handling.** Save per-user default addresses so customers don't retype them.
- **Markup / pricing UI.** Right now markup is a flat percent in `.env`. Could be per-restaurant.
- **Admin commands.** `/refund <checkout_id>`, `/mark-placed <checkout_id>` for cleaning up `needs_check` rows.

## Rules this project enforces

- No secrets in code or logs. `.env` is gitignored.
- Never log full card numbers — `yonder.client.mask_card` enforces this.
- Only retry when the API says it's safe. Confirm never auto-retries on unknown outcome.
- Checkout state machine: `pending → placed | cancelled | expired`.
