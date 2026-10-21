"""The paid /order pipeline: price -> pay -> confirm.

State machine per order:
    priced       Yonder checkout created, Stripe session created, awaiting payment.
    paid         Stripe reports paid; we are placing the order on Yonder.
    placed       Confirmed on Yonder; tracking URL sent to customer.
    refunded     Confirm failed (known outcome); Stripe refund issued.
    needs_check  Confirm outcome unknown; NO refund, flagged for owner.
    expired      Payment timed out; Yonder checkout cancelled.
    cancelled    Customer cancelled the Stripe session.
    failed       Something else went wrong (pricing failed, Stripe failed).

Every state transition is persisted via OrderStore so the bot can resume
pending orders after a restart. If the bot was down while a payment
completed, resume() will pick it up and finish placing the order.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import discord

from payments import StripeClient, StripeError
from payments.stripe_client import dollars_to_cents
from storage import OrderStore, StoredOrder
from yonder import OutcomeUnknownError, YonderAPIError, YonderClient, YonderError

log = logging.getLogger("order_flow")

PAYMENT_POLL_INTERVAL_S = 3.0
PAYMENT_TIMEOUT_S = 10 * 60  # 10 minutes


@dataclass
class OrderContext:
    discord_user_id: int
    cart_code: str
    address: Optional[str]
    tip: Optional[float]
    checkout_id: Optional[str] = None
    stripe_session_id: Optional[str] = None
    stripe_payment_intent: Optional[str] = None
    state: str = "pending"
    tracking_url: Optional[str] = None
    order_id: Optional[str] = None
    error_message: Optional[str] = None
    yonder_total: Optional[float] = None
    charged_cents: Optional[int] = None
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_stored(cls, s: StoredOrder) -> "OrderContext":
        return cls(
            discord_user_id=s.discord_user_id,
            cart_code=s.cart_code,
            address=s.address,
            tip=s.tip,
            checkout_id=s.checkout_id,
            stripe_session_id=s.stripe_session_id,
            stripe_payment_intent=s.stripe_payment_intent,
            state=s.state,
            tracking_url=s.tracking_url,
            order_id=s.order_id,
            error_message=s.error_message,
            yonder_total=s.yonder_total,
            charged_cents=s.charged_cents,
        )


class OrderFlow:
    """Wires one /order invocation from pricing through confirm."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        store: OrderStore,
        markup_percent: float = 0.0,
        owner_discord_id: int | None = None,
    ):
        self._bot = bot
        self._store = store
        self._markup = max(0.0, float(markup_percent))
        self._owner_id = owner_discord_id

    # ================================================================= public
    async def start(
        self,
        interaction: discord.Interaction,
        cart_code: str,
        address: Optional[str],
        tip: Optional[float],
    ) -> None:
        """Kick off a fresh /order. Replies ephemerally and runs the rest in the background."""
        ctx = OrderContext(
            discord_user_id=interaction.user.id,
            cart_code=cart_code,
            address=address,
            tip=tip,
        )

        # Step 1: price on Yonder.
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
            await interaction.followup.send(f"Could not price that cart: {e.human_message()}", ephemeral=True)
            return
        except YonderError as e:
            await interaction.followup.send(f"Could not reach Yonder: {e}", ephemeral=True)
            return

        ctx.checkout_id = checkout["id"]
        price = checkout.get("price") or {}
        yonder_total = float(price.get("total", 0))
        ctx.yonder_total = yonder_total
        if yonder_total <= 0:
            await interaction.followup.send("Yonder returned a zero total — not charging.", ephemeral=True)
            await self._safe_cancel(ctx.checkout_id)
            return

        charge_amount = yonder_total * (1 + self._markup / 100.0)
        charge_cents = dollars_to_cents(charge_amount)
        ctx.charged_cents = charge_cents

        restaurant = checkout.get("restaurant") or "your order"
        product_name = f"Order from {restaurant}"

        # Step 2: create Stripe Checkout Session.
        try:
            stripe_client = StripeClient()
            session = await stripe_client.create_checkout_session(
                amount_cents=charge_cents,
                product_name=product_name,
                metadata={
                    "yonder_checkout_id": ctx.checkout_id,
                    "discord_user_id": str(ctx.discord_user_id),
                },
            )
        except StripeError as e:
            await interaction.followup.send(f"Payment setup failed: {e}", ephemeral=True)
            await self._safe_cancel(ctx.checkout_id)
            return

        ctx.stripe_session_id = session.id
        ctx.state = "priced"

        # Step 3: persist. From here on, the bot can restart safely.
        await self._store.insert(
            checkout_id=ctx.checkout_id,
            discord_user_id=ctx.discord_user_id,
            cart_code=ctx.cart_code,
            address=ctx.address,
            tip=ctx.tip,
            stripe_session_id=ctx.stripe_session_id,
            state=ctx.state,
            yonder_total=ctx.yonder_total,
            charged_cents=ctx.charged_cents,
        )

        # Step 4: show the pay link.
        embed = self._priced_embed(ctx, checkout, charge_cents, session.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Step 5: run the rest off the request handler.
        self._bot.loop.create_task(
            self._run_pipeline(ctx),
            name=f"order:{ctx.checkout_id}",
        )

    async def resume_pending(self) -> int:
        """Load every non-terminal order from the DB and resume its pipeline.

        Call this once at bot startup, after login.
        """
        live = await self._store.list_live()
        for stored in live:
            ctx = OrderContext.from_stored(stored)
            log.info("resuming order %s in state %s", ctx.checkout_id, ctx.state)
            self._bot.loop.create_task(
                self._run_pipeline(ctx, resumed=True),
                name=f"order:{ctx.checkout_id}:resumed",
            )
        return len(live)

    # ============================================================== pipeline
    async def _run_pipeline(self, ctx: OrderContext, *, resumed: bool = False) -> None:
        """The resumable background path. Branches on current state."""
        stripe_client = StripeClient()
        try:
            if ctx.state == "priced":
                paid = await self._await_payment(ctx, stripe_client)
                if not paid:
                    await self._handle_payment_timeout(ctx, stripe_client)
                    return
            if ctx.state == "paid":
                await self._place_on_yonder(ctx, stripe_client, resumed=resumed)
        except Exception as e:
            log.exception("unhandled error in pipeline for %s: %s", ctx.checkout_id, e)
            await self._dm_owner(
                f"**Unhandled pipeline error** for checkout `{ctx.checkout_id}`: {e}\n"
                f"User: <@{ctx.discord_user_id}>. State at crash: `{ctx.state}`."
            )

    async def _await_payment(self, ctx: OrderContext, stripe_client: StripeClient) -> bool:
        """Poll Stripe until paid or timeout. Returns True on paid."""
        waited = 0.0
        while waited < PAYMENT_TIMEOUT_S:
            await asyncio.sleep(PAYMENT_POLL_INTERVAL_S)
            waited += PAYMENT_POLL_INTERVAL_S
            try:
                status = await stripe_client.get_session_status(ctx.stripe_session_id)
            except StripeError as e:
                log.warning("stripe poll error for %s: %s", ctx.checkout_id, e)
                continue

            if status.payment_status == "paid":
                ctx.stripe_payment_intent = status.payment_intent
                ctx.state = "paid"
                await self._store.update(
                    ctx.checkout_id,
                    state="paid",
                    stripe_payment_intent=status.payment_intent,
                )
                return True
            if status.status == "expired":
                return False
        return False

    async def _place_on_yonder(
        self,
        ctx: OrderContext,
        stripe_client: StripeClient,
        *,
        resumed: bool = False,
    ) -> None:
        """Reprice (hold may have aged), confirm, then DM the customer."""
        try:
            async with YonderClient() as yc:
                try:
                    await yc.reprice_checkout(ctx.checkout_id)
                except YonderAPIError as e:
                    # Hold expired or similar — never placed.
                    await self._refund_and_report(
                        ctx, stripe_client,
                        reason=f"Reprice failed: {e.human_message()}",
                    )
                    return

                try:
                    placed = await yc.confirm_checkout(ctx.checkout_id)
                except OutcomeUnknownError as e:
                    await self._flag_for_human(ctx, str(e))
                    return
                except YonderAPIError as e:
                    await self._refund_and_report(
                        ctx, stripe_client,
                        reason=f"Order confirmation failed: {e.human_message()}",
                    )
                    return
        except YonderError as e:
            await self._flag_for_human(ctx, f"Could not reach Yonder on confirm: {e}")
            return

        ctx.tracking_url = placed.get("tracking") or placed.get("tracking_url")
        ctx.order_id = placed.get("order_id")
        ctx.state = "placed"
        await self._store.update(
            ctx.checkout_id,
            state="placed",
            tracking_url=ctx.tracking_url,
            order_id=ctx.order_id,
        )
        prefix = "**Resumed order placed.** " if resumed else ""
        await self._dm_user(
            ctx.discord_user_id,
            content=(
                f"{prefix}Your order is in! Track it here: {ctx.tracking_url}\n"
                f"Order id: `{ctx.order_id}`"
            ),
        )
        log.info("order placed: checkout=%s order=%s resumed=%s", ctx.checkout_id, ctx.order_id, resumed)

    # ============================================================== outcomes
    async def _handle_payment_timeout(
        self,
        ctx: OrderContext,
        stripe_client: StripeClient,
    ) -> None:
        ctx.state = "expired"
        await stripe_client.expire_session(ctx.stripe_session_id)
        await self._safe_cancel(ctx.checkout_id)
        await self._store.update(ctx.checkout_id, state="expired")
        await self._dm_user(
            ctx.discord_user_id,
            content=(
                "Your order timed out before payment. Nothing was charged. "
                "Run `/order` again to try again."
            ),
        )

    async def _refund_and_report(
        self,
        ctx: OrderContext,
        stripe_client: StripeClient,
        reason: str,
    ) -> None:
        refund_id: Optional[str] = None
        if ctx.stripe_payment_intent:
            try:
                refund_id = await stripe_client.refund_payment_intent(
                    ctx.stripe_payment_intent, reason=reason
                )
            except StripeError as e:
                log.error("refund failed for %s: %s", ctx.stripe_payment_intent, e)
                await self._store.update(
                    ctx.checkout_id,
                    state="needs_check",
                    error_message=f"Refund failed: {e}",
                )
                await self._dm_owner(
                    f"**Refund failed** for payment `{ctx.stripe_payment_intent}` "
                    f"(checkout `{ctx.checkout_id}`). Reason: {reason}. Stripe error: {e}"
                )
                ctx.state = "needs_check"
                await self._dm_user(
                    ctx.discord_user_id,
                    content=(
                        f"Your order could not be placed ({reason}) and the automatic refund failed. "
                        "The owner has been notified and will refund you manually."
                    ),
                )
                return

        ctx.state = "refunded"
        await self._safe_cancel(ctx.checkout_id)
        await self._store.update(
            ctx.checkout_id,
            state="refunded",
            error_message=reason,
        )
        await self._dm_user(
            ctx.discord_user_id,
            content=(
                f"Your order could not be placed: {reason}\n"
                f"You've been fully refunded (Stripe refund `{refund_id or 'pending'}`)."
            ),
        )

    async def _flag_for_human(self, ctx: OrderContext, reason: str) -> None:
        """Confirm outcome unknown — the order MAY have been placed. No refund."""
        ctx.state = "needs_check"
        ctx.error_message = reason
        await self._store.update(
            ctx.checkout_id,
            state="needs_check",
            error_message=reason,
        )
        await self._dm_user(
            ctx.discord_user_id,
            content=(
                "Your payment went through, but we could not confirm whether the order "
                "actually placed. The owner has been notified and will verify within a few "
                "minutes. Do not run `/order` again for this cart."
            ),
        )
        await self._dm_owner(
            f"**ORDER NEEDS HUMAN CHECK**\n"
            f"Checkout: `{ctx.checkout_id}`\n"
            f"Discord user: <@{ctx.discord_user_id}> (`{ctx.discord_user_id}`)\n"
            f"Stripe payment intent: `{ctx.stripe_payment_intent}`\n"
            f"Reason: {reason}\n"
            f"Check `GET /v1/orders` in Yonder before refunding."
        )
        log.warning(
            "needs_check: checkout=%s user=%s reason=%s",
            ctx.checkout_id, ctx.discord_user_id, reason,
        )

    # ================================================================ helpers
    async def _safe_cancel(self, checkout_id: Optional[str]) -> None:
        if not checkout_id:
            return
        try:
            async with YonderClient() as yc:
                await yc.cancel_checkout(checkout_id)
        except YonderError as e:
            log.info("checkout %s not cancelled cleanly: %s", checkout_id, e)

    async def _dm_user(self, user_id: int, *, content: str) -> None:
        try:
            user = self._bot.get_user(user_id) or await self._bot.fetch_user(user_id)
            await user.send(content)
        except discord.DiscordException as e:
            log.warning("could not DM user %s: %s", user_id, e)

    async def _dm_owner(self, content: str) -> None:
        if not self._owner_id:
            log.warning("OWNER_DISCORD_ID not set — owner alert dropped: %s", content.replace("\n", " | "))
            return
        await self._dm_user(self._owner_id, content=content)

    def _priced_embed(
        self,
        ctx: OrderContext,
        checkout: dict,
        charge_cents: int,
        pay_url: str,
    ) -> discord.Embed:
        restaurant = checkout.get("restaurant") or "your order"
        price = checkout.get("price") or {}
        total = charge_cents / 100.0
        embed = discord.Embed(
            title=f"Ready to order — {restaurant}",
            description=f"[Click here to pay — ${total:.2f}]({pay_url})",
            color=0x2ecc71,
        )
        subtotal = price.get("subtotal")
        service = price.get("service")
        delivery = price.get("delivery")
        tax = price.get("tax")
        tip = price.get("tip")
        yonder_total = ctx.yonder_total or 0
        details = []
        if subtotal is not None:
            details.append(f"Subtotal: ${subtotal:.2f}")
        if service is not None:
            details.append(f"Service: ${service:.2f}")
        if delivery is not None and delivery:
            details.append(f"Delivery: ${delivery:.2f}")
        if tax is not None:
            details.append(f"Tax: ${tax:.2f}")
        if tip is not None and tip:
            details.append(f"Tip: ${tip:.2f}")
        details.append(f"**Yonder total: ${yonder_total:.2f}**")
        if self._markup > 0:
            details.append(f"Service fee (+{self._markup:.0f}%): ${total - yonder_total:.2f}")
            details.append(f"**You pay: ${total:.2f}**")
        embed.add_field(name="Breakdown", value="\n".join(details), inline=False)

        ref_problem = checkout.get("referral_problem")
        if ref_problem:
            embed.add_field(name="heads up", value=ref_problem, inline=False)

        embed.set_footer(text="You have 10 minutes to pay. Tracking link is DM'd on confirmation.")
        return embed


def build_from_env(bot: discord.Client, store: OrderStore) -> OrderFlow:
    """Construct the flow with values from environment variables."""
    markup = float(os.environ.get("STRIPE_MARKUP_PERCENT") or 0)
    owner_raw = os.environ.get("OWNER_DISCORD_ID") or ""
    owner_id: int | None
    try:
        owner_id = int(owner_raw) if owner_raw.strip() else None
    except ValueError:
        owner_id = None
    return OrderFlow(
        bot=bot,
        store=store,
        markup_percent=markup,
        owner_discord_id=owner_id,
    )
