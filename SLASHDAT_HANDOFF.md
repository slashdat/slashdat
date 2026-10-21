# SlashDat — Project Handoff

## What this is
A Discord bot that lets users place food delivery orders through the **Yonder API** (https://yonderdrop.com/v1), paying the bot owner (Matt) via Stripe. Yonder brokers orders onto Wonder.

## Stack
- **Python 3.11+**
- **discord.py** — Discord slash-command framework
- **httpx** — async HTTP client for calling Yonder
- **python-dotenv** — load secrets from `.env`
- **Stripe** — payments (not wired yet; stub for future)

## Key decisions already made
- Chose Python over TypeScript for readability (owner is new to coding).
- API key lives in `.env`, never in code or logs.
- Retry logic: only when `retryable: true`. Never auto-retry a `confirm` with `outcome_known: false`.
- Card numbers: last 4 only, ever.
- Checkouts modeled as a state machine: `pending → placed | cancelled | expired`.

## Yonder API essentials (don't make Claude re-read the whole spec)
- Base: `https://yonderdrop.com/v1`
- Auth: `Authorization: Bearer yd_live_...` (header only, never query string)
- Every response has `ok: bool`. Failures include `code`, `error` (human sentence), `retryable`.
- Place order = two calls: `POST /v1/checkouts` then `POST /v1/checkouts/{id}/confirm`.
- Confirm idempotency: re-confirm the SAME checkout id. Never create a new one on retry.
- `FORTER_DECLINE` first time: PATCH a new card onto same checkout and re-confirm. Second decline: build a new checkout on another chain.
- Rate limits per key — honour `Retry-After`.

## Current status
Working `/order` flow (price → Stripe Checkout Session → poll for payment → reprice → confirm → DM tracking) with refund-on-failure, human-escalation on unknown-outcome, and SQLite persistence that survives bot restarts.

## Order flow state machine (in `order_flow.py`)
`priced → paid → placed`
Branches:
- `expired` — payment timed out, Yonder checkout cancelled
- `refunded` — confirm failed with known outcome, Stripe refund issued
- `needs_check` — outcome unknown; NO refund; owner DM'd for manual verification
- `failed` — pricing or Stripe setup failed before any charge

Every state transition is persisted to SQLite (`slashdat.db`). On bot startup, orders in `priced` or `paid` are re-spawned from the DB and the pipeline continues. Terminal states stay as history.

## Storage schema
Single `orders` table in `storage.py`, keyed on `checkout_id`. Includes stripe_session_id, stripe_payment_intent, tracking_url, order_id, state, created_at/updated_at. No card data ever stored.

## Deployment
Configured for Railway via Nixpacks:
- `Procfile` → `worker: python bot.py` (no HTTP, no public URL)
- `runtime.txt` → `python-3.11.11`
- Persistent volume mounted at `/data`, with `DATABASE_PATH=/data/slashdat.db` set in Railway variables.
Full step-by-step is in the README under "Deploying to Railway".

## Next moves (not yet done)
1. Stripe webhooks instead of polling (needs a public URL — flip Railway "Public Networking" on first).
2. FORTER_DECLINE card-swap flow inside /order.
3. Per-user saved addresses.
4. Admin commands for cleaning up `needs_check` rows.

## Safety rules permanent to this project
- Never commit `.env` or any API key.
- Never log full card numbers.
- Never auto-retry an outcome-unknown confirm.
- Surface `error` sentences verbatim to users; branch on `code` internally.
