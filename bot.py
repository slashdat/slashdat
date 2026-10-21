"""SlashDat Discord bot — base scaffold.

Run:
    python bot.py

Required env vars (put them in .env):
    DISCORD_TOKEN         Discord bot token
    YONDER_API_KEY        Yonder API key (yd_live_...)
    STRIPE_SECRET_KEY     Stripe secret key (sk_test_... or sk_live_...)
    DISCORD_GUILD_IDS     (optional) comma-separated server ids for fast dev sync
    STRIPE_SUCCESS_URL    (optional) where Stripe sends paid customers
    STRIPE_CANCEL_URL     (optional) where Stripe sends cancelled customers
    STRIPE_MARKUP_PERCENT (optional) % to add on top of Yonder's price
    OWNER_DISCORD_ID      (optional) your Discord user id for human-check alerts
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from order_flow import OrderFlow, build_from_env
from storage import OrderStore
from yonder import YonderAPIError, YonderClient, YonderError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("slashdat")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_IDS = [
    int(x) for x in (os.environ.get("DISCORD_GUILD_IDS") or "").split(",") if x.strip()
]


class SlashDatBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store: OrderStore | None = None
        self.order_flow: OrderFlow | None = None
        self._resumed = False

    async def setup_hook(self) -> None:
        db_path = os.environ.get("DATABASE_PATH") or "slashdat.db"
        self.store = OrderStore(db_path)
        await self.store.init()
        self.order_flow = build_from_env(self, self.store)
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("synced commands to guild %s", gid)
        else:
            await self.tree.sync()
            log.info("synced commands globally (may take up to 1 hour)")

    async def on_ready(self) -> None:
        if self._resumed:
            return
        self._resumed = True
        log.info("logged in as %s", self.user)
        if self.order_flow is not None:
            resumed = await self.order_flow.resume_pending()
            if resumed:
                log.info("resumed %d in-flight order(s) from the database", resumed)


bot = SlashDatBot()


# ---------------------------------------------------------------- commands
@bot.tree.command(name="ping", description="Check the bot is alive.")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong — {latency_ms}ms", ephemeral=True)


@bot.tree.command(name="yonder-health", description="Is the Yonder API up?")
async def yonder_health(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        async with YonderClient() as yc:
            await yc.health()
        await interaction.followup.send("Yonder API is up.", ephemeral=True)
    except YonderAPIError as e:
        await interaction.followup.send(f"Yonder said: {e.human_message()}", ephemeral=True)
    except YonderError as e:
        await interaction.followup.send(f"Could not reach Yonder: {e}", ephemeral=True)


@bot.tree.command(name="yonder-status", description="Your Yonder account status and daily plan.")
async def yonder_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        async with YonderClient() as yc:
            me = await yc.me()
            plan = await yc.plan()
    except YonderAPIError as e:
        await interaction.followup.send(f"Yonder said: {e.human_message()}", ephemeral=True)
        return
    except YonderError as e:
        await interaction.followup.send(f"Could not reach Yonder: {e}", ephemeral=True)
        return

    embed = discord.Embed(title="Yonder status", color=0x2ecc71)
    for k, v in (me or {}).items():
        if k == "ok":
            continue
        embed.add_field(name=k, value=f"`{v}`", inline=True)
    embed.add_field(
        name="plan",
        value=(
            f"tier: `{plan.get('tier')}`\n"
            f"used today: `{plan.get('used')}`\n"
            f"remaining: `{plan.get('remaining')}`\n"
            f"resets at: `{plan.get('resets_at')}`"
        ),
        inline=False,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="price",
    description="Price a Wonder cart (does not place the order).",
)
@app_commands.describe(
    cart_code="Wonder cart code, e.g. ABC123",
    address="Delivery address (omit for pickup)",
    tip="Driver tip in dollars (optional)",
)
async def price(
    interaction: discord.Interaction,
    cart_code: str,
    address: Optional[str] = None,
    tip: Optional[float] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        async with YonderClient() as yc:
            kwargs: dict = {}
            if address:
                kwargs["address"] = address
            else:
                kwargs["fulfillment"] = "PICKUP"
            if tip is not None:
                kwargs["tip"] = tip
            checkout = await yc.create_checkout(cart_code=cart_code, **kwargs)
    except YonderAPIError as e:
        await interaction.followup.send(f"{e.human_message()}", ephemeral=True)
        return
    except YonderError as e:
        await interaction.followup.send(f"Could not reach Yonder: {e}", ephemeral=True)
        return

    price = checkout.get("price") or {}
    restaurant = checkout.get("restaurant") or "the restaurant"
    expires_in = checkout.get("expires_in")

    embed = discord.Embed(
        title=f"Cart priced — {restaurant}",
        description=f"Checkout id: `{checkout.get('id')}`",
        color=0x3498db,
    )
    for key in ("subtotal", "service", "delivery", "tax", "tip", "referral", "credit", "total"):
        if key in price:
            embed.add_field(name=key, value=f"${price[key]:.2f}", inline=True)
    if expires_in:
        embed.set_footer(text=f"Hold expires in {expires_in}s. Confirm before then.")

    ref_problem = checkout.get("referral_problem")
    if ref_problem:
        embed.add_field(name="heads up", value=ref_problem, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="myorders",
    description="Your 10 most recent orders through this bot.",
)
async def myorders(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    if bot.store is None:
        await interaction.followup.send("Database not ready yet.", ephemeral=True)
        return
    rows = await bot.store.list_for_user(interaction.user.id, limit=10)
    if not rows:
        await interaction.followup.send("No orders yet. Run `/order` to place one.", ephemeral=True)
        return
    embed = discord.Embed(title="Your recent orders", color=0x9b59b6)
    for row in rows:
        line = f"state: `{row.state}`"
        if row.order_id:
            line += f" · order `{row.order_id}`"
        if row.tracking_url:
            line += f" · [track]({row.tracking_url})"
        if row.charged_cents:
            line += f" · ${row.charged_cents / 100:.2f}"
        embed.add_field(name=f"`{row.checkout_id}` ({row.cart_code})", value=line, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="order",
    description="Price a cart, charge the customer, then place the order.",
)
@app_commands.describe(
    cart_code="Wonder cart code, e.g. ABC123",
    address="Delivery address (omit for pickup)",
    tip="Driver tip in dollars (optional)",
)
async def order(
    interaction: discord.Interaction,
    cart_code: str,
    address: Optional[str] = None,
    tip: Optional[float] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if bot.order_flow is None:
        await interaction.followup.send("Order flow not initialised yet. Try again in a moment.", ephemeral=True)
        return
    await bot.order_flow.start(interaction, cart_code=cart_code, address=address, tip=tip)


# ---------------------------------------------------------------- entry
def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not os.environ.get("YONDER_API_KEY"):
        raise SystemExit("YONDER_API_KEY is not set. Copy .env.example to .env and fill it in.")
    if not os.environ.get("STRIPE_SECRET_KEY"):
        raise SystemExit("STRIPE_SECRET_KEY is not set. Copy .env.example to .env and fill it in.")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
