"""Yonder API error types.

Branch on `code` in your logic. Show `error` to humans.
"""
from __future__ import annotations


class YonderError(Exception):
    """Base class for every error raised by the Yonder client."""


class YonderAPIError(YonderError):
    """A structured error returned by the Yonder API (ok=false response)."""

    def __init__(
        self,
        code: str,
        error: str,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        payload: dict | None = None,
    ):
        super().__init__(f"{code}: {error}")
        self.code = code
        self.error = error
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.payload = payload or {}

    def human_message(self) -> str:
        """Sentence suitable to show a user verbatim."""
        return self.error


class OutcomeUnknownError(YonderError):
    """Confirm failed with outcome_known=false. Order MAY have been placed.

    Never auto-retry. Surface to a human.
    """

    def __init__(self, checkout_id: str, message: str, payload: dict | None = None):
        super().__init__(message)
        self.checkout_id = checkout_id
        self.payload = payload or {}
