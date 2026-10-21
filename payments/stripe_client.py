"""Thin async-friendly wrapper around Stripe for one-off order payments.

Stripe's SDK is sync. We run its calls in a thread so we don't block the bot's
event loop. For the base scaffold we poll Stripe Checkout Sessions until paid
(no webhooks needed — simpler for a bot with no public URL yet).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import stripe

log = logging.getLogger("stripe")


class StripeError(Exception):
    """Raised when Stripe refuses or errors out."""


@dataclass
class CheckoutSession:
    id: str
    url: str
    amount_cents: int


@dataclass
class SessionStatus:
    id: str
    payment_status: str  # "paid" | "unpaid" | "no_payment_required"
    status: str          # "open" | "complete" | "expired"
    payment_intent: str | None


class StripeClient:
    def __init__(
        self,
        api_key: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ):
        key = api_key or os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            raise StripeError("STRIPE_SECRET_KEY is not set. Put it in your .env file.")
        stripe.api_key = key
        self._success_url = success_url or os.environ.get("STRIPE_SUCCESS_URL") or "https://discord.com"
        self._cancel_url = cancel_url or os.environ.get("STRIPE_CANCEL_URL") or "https://discord.com"

    async def create_checkout_session(
        self,
        amount_cents: int,
        product_name: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> CheckoutSession:
        """Create a one-off Stripe Checkout Session. Returns the hosted pay URL."""
        if amount_cents <= 0:
            raise StripeError("Stripe amount must be > 0 cents.")

        def _create() -> stripe.checkout.Session:
            try:
                return stripe.checkout.Session.create(
                    mode="payment",
                    payment_method_types=["card"],
                    line_items=[
                        {
                            "price_data": {
                                "currency": "usd",
                                "product_data": {"name": product_name[:250]},
                                "unit_amount": amount_cents,
                            },
                            "quantity": 1,
                        }
                    ],
                    success_url=self._success_url,
                    cancel_url=self._cancel_url,
                    metadata=metadata or {},
                )
            except stripe.StripeError as e:
                raise StripeError(f"Stripe refused the session: {e.user_message or e}") from e

        session = await asyncio.to_thread(_create)
        return CheckoutSession(id=session.id, url=session.url, amount_cents=amount_cents)

    async def get_session_status(self, session_id: str) -> SessionStatus:
        def _get() -> stripe.checkout.Session:
            try:
                return stripe.checkout.Session.retrieve(session_id)
            except stripe.StripeError as e:
                raise StripeError(f"Stripe lookup failed: {e.user_message or e}") from e

        s = await asyncio.to_thread(_get)
        return SessionStatus(
            id=s.id,
            payment_status=s.payment_status,
            status=s.status,
            payment_intent=s.payment_intent,
        )

    async def expire_session(self, session_id: str) -> None:
        """Close an unpaid session so the customer can't pay after we gave up."""
        def _expire() -> None:
            try:
                stripe.checkout.Session.expire(session_id)
            except stripe.StripeError as e:
                log.warning("stripe: could not expire session %s: %s", session_id, e)

        await asyncio.to_thread(_expire)

    async def refund_payment_intent(self, payment_intent: str, reason: str | None = None) -> str:
        """Full refund. Returns the refund id."""
        def _refund() -> stripe.Refund:
            try:
                return stripe.Refund.create(
                    payment_intent=payment_intent,
                    reason="requested_by_customer",
                    metadata={"note": (reason or "")[:500]},
                )
            except stripe.StripeError as e:
                raise StripeError(f"Stripe refund failed: {e.user_message or e}") from e

        refund = await asyncio.to_thread(_refund)
        return refund.id


def dollars_to_cents(dollars: float) -> int:
    """Convert a dollar float to integer cents, rounding half-up."""
    return int(round(dollars * 100))
