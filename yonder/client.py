"""Async Yonder API client.

Design rules baked in:
- API key read from env, never logged.
- Retry only when the API says `retryable: true`. Honour Retry-After.
- Confirm is never auto-retried when outcome_known is false — raised as
  OutcomeUnknownError for a human to handle.
- Card numbers are never logged. Only last4 ever appears in client output.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx

from .errors import OutcomeUnknownError, YonderAPIError, YonderError

log = logging.getLogger("yonder")

DEFAULT_BASE_URL = "https://yonderdrop.com/v1"
DEFAULT_TIMEOUT = 30.0
CONFIRM_TIMEOUT = 450.0  # confirm can take up to 7 minutes
MAX_RETRIES = 4
BACKOFF_BASE = 0.5
BACKOFF_CAP = 20.0


class YonderClient:
    """Thin async wrapper over the Yonder REST API.

    Usage:
        async with YonderClient() as yc:
            me = await yc.me()
            checkout = await yc.create_checkout(cart_code="ABC123", address="...")
            placed = await yc.confirm_checkout(checkout["id"])
            print(placed["tracking"])
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        key = api_key or os.environ.get("YONDER_API_KEY")
        if not key:
            raise YonderError(
                "YONDER_API_KEY is not set. Put it in your .env file."
            )
        self._api_key = key
        self._base_url = (base_url or os.environ.get("YONDER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> "YonderClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "slashdat/0.1",
            },
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ core request
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
        allow_retry: bool = True,
    ) -> dict[str, Any]:
        if self._client is None:
            raise YonderError("YonderClient must be used inside `async with`.")

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    timeout=timeout or self._timeout,
                )
            except httpx.TimeoutException as e:
                # Network timeout — never retry a confirm here; the caller handles that.
                raise YonderError(f"Request timed out: {e}") from e
            except httpx.HTTPError as e:
                raise YonderError(f"Network error talking to Yonder: {e}") from e

            try:
                body = resp.json()
            except ValueError:
                raise YonderError(
                    f"Yonder returned non-JSON ({resp.status_code}): {resp.text[:200]!r}"
                )

            if body.get("ok") is True:
                return body

            # Structured failure.
            code = body.get("code", "UNKNOWN")
            error_msg = body.get("error", "Unknown error from Yonder.")
            retryable = bool(body.get("retryable", False))
            retry_after = body.get("retry_after")
            if retry_after is None:
                hdr = resp.headers.get("Retry-After")
                if hdr:
                    try:
                        retry_after = float(hdr)
                    except ValueError:
                        retry_after = None

            if allow_retry and retryable and attempt <= MAX_RETRIES:
                delay = _backoff_delay(attempt, retry_after)
                log.info(
                    "yonder: retryable error code=%s attempt=%d sleeping=%.2fs",
                    code, attempt, delay,
                )
                await asyncio.sleep(delay)
                continue

            raise YonderAPIError(
                code=code,
                error=error_msg,
                retryable=retryable,
                status_code=resp.status_code,
                retry_after=retry_after,
                payload=body,
            )

    # ------------------------------------------------------------------ account / health
    async def health(self) -> dict:
        """Public health check — needs no key, but we send it anyway for a round-trip."""
        return await self._request("GET", "/health")

    async def me(self) -> dict:
        return await self._request("GET", "/me")

    async def status(self) -> dict:
        return await self._request("GET", "/status")

    async def plan(self) -> dict:
        return await self._request("GET", "/plan")

    # ------------------------------------------------------------------ checkouts
    async def create_checkout(
        self,
        cart_code: str,
        address: str | None = None,
        *,
        tip: float | None = None,
        notes: str | None = None,
        dropoff: str | None = None,
        fulfillment: str | None = None,
        card: str | None = None,
        chain: str | None = None,
        use_credit: bool | None = None,
        jig: bool | None = None,
    ) -> dict:
        """POST /v1/checkouts — price a cart. Charges nothing. Holds a chain for 5 min."""
        payload: dict[str, Any] = {"cart_code": cart_code}
        if address is not None:
            payload["address"] = address
        if tip is not None:
            payload["tip"] = tip
        if notes is not None:
            payload["notes"] = notes
        if dropoff is not None:
            payload["dropoff"] = dropoff
        if fulfillment is not None:
            payload["fulfillment"] = fulfillment
        if card is not None:
            payload["card"] = card
        if chain is not None:
            payload["chain"] = chain
        if use_credit is not None:
            payload["use_credit"] = use_credit
        if jig is not None:
            payload["jig"] = jig
        return await self._request("POST", "/checkouts", json=payload)

    async def get_checkout(self, checkout_id: str) -> dict:
        return await self._request("GET", f"/checkouts/{checkout_id}")

    async def patch_checkout(self, checkout_id: str, **changes: Any) -> dict:
        """PATCH /v1/checkouts/{id}. Allowed: tip, notes, dropoff, fulfillment, card, address."""
        if not changes:
            raise YonderError("patch_checkout requires at least one change.")
        allowed = {"tip", "notes", "dropoff", "fulfillment", "card", "address"}
        bad = set(changes) - allowed
        if bad:
            raise YonderError(f"patch_checkout got unsupported fields: {sorted(bad)}")
        return await self._request("PATCH", f"/checkouts/{checkout_id}", json=changes)

    async def set_checkout_name(self, checkout_id: str, name: str) -> dict:
        return await self._request("POST", f"/checkouts/{checkout_id}/name", json={"name": name})

    async def reprice_checkout(self, checkout_id: str) -> dict:
        return await self._request("POST", f"/checkouts/{checkout_id}/reprice")

    async def cancel_checkout(self, checkout_id: str) -> dict:
        return await self._request("POST", f"/checkouts/{checkout_id}/cancel")

    async def confirm_checkout(self, checkout_id: str, *, async_mode: bool = False) -> dict:
        """POST /v1/checkouts/{id}/confirm — place the order.

        CRITICAL: never auto-retry. If confirm raises YonderAPIError and the
        payload has outcome_known=false, we re-raise as OutcomeUnknownError
        so the caller surfaces it to a human. A network timeout is treated
        the same way.
        """
        if self._client is None:
            raise YonderError("YonderClient must be used inside `async with`.")

        params = {"async": "true"} if async_mode else None
        try:
            # allow_retry=False — confirm never retries at this layer.
            return await self._request(
                "POST",
                f"/checkouts/{checkout_id}/confirm",
                params=params,
                timeout=CONFIRM_TIMEOUT,
                allow_retry=False,
            )
        except YonderAPIError as e:
            outcome_known = e.payload.get("outcome_known")
            if outcome_known is False:
                raise OutcomeUnknownError(
                    checkout_id=checkout_id,
                    message=(
                        f"Confirm failed with unknown outcome for {checkout_id}. "
                        f"The order may have been placed. {e.error}"
                    ),
                    payload=e.payload,
                ) from e
            raise
        except YonderError as e:
            # Network/timeout — outcome is unknown by definition.
            raise OutcomeUnknownError(
                checkout_id=checkout_id,
                message=(
                    f"Confirm connection failed for {checkout_id}: {e}. "
                    "The order may have been placed. Check /v1/orders before retrying."
                ),
            ) from e

    async def schedule_checkout(self, checkout_id: str, at: str | int) -> dict:
        return await self._request(
            "POST", f"/checkouts/{checkout_id}/schedule", json={"at": at}
        )

    # ------------------------------------------------------------------ scheduled
    async def list_scheduled(self) -> dict:
        return await self._request("GET", "/scheduled")

    async def get_scheduled(self, sch_id: str) -> dict:
        return await self._request("GET", f"/scheduled/{sch_id}")

    async def cancel_scheduled(self, sch_id: str) -> dict:
        return await self._request("DELETE", f"/scheduled/{sch_id}")

    # ------------------------------------------------------------------ orders
    async def list_orders(self) -> dict:
        return await self._request("GET", "/orders")

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/orders/{order_id}")

    async def track(self, ref: str) -> dict:
        return await self._request("GET", f"/track/{ref}")

    # ------------------------------------------------------------------ jobs
    async def get_job(self, job_id: str) -> dict:
        return await self._request("GET", f"/jobs/{job_id}")

    async def list_jobs(self) -> dict:
        return await self._request("GET", "/jobs")

    async def poll_job(
        self,
        job_id: str,
        *,
        interval: float = 2.0,
        max_wait: float = 600.0,
    ) -> dict:
        """Poll a job until done or failed. Returns the final job payload."""
        waited = 0.0
        while True:
            job = await self.get_job(job_id)
            state = job.get("state")
            if state in ("done", "failed") or job.get("done"):
                return job
            if waited >= max_wait:
                raise YonderError(f"Job {job_id} not done after {max_wait:.0f}s")
            await asyncio.sleep(interval)
            waited += interval


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, capped. Honours Retry-After when present."""
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), BACKOFF_CAP)
    base = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def mask_card(card_number: str) -> str:
    """Return a safe-to-log representation of a card number: ****1234."""
    digits = "".join(ch for ch in card_number if ch.isdigit())
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"
