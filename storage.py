"""SQLite persistence for in-flight and historical orders.

Live states that must survive a restart: priced, paid.
Terminal states (placed, refunded, needs_check, expired, cancelled, failed) stay
for history and lookups.

We never store raw card data here. We only ever hold Stripe ids and Yonder ids.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import aiosqlite

log = logging.getLogger("storage")

LIVE_STATES = ("priced", "paid")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    checkout_id            TEXT PRIMARY KEY,
    discord_user_id        INTEGER NOT NULL,
    cart_code              TEXT NOT NULL,
    address                TEXT,
    tip                    REAL,
    stripe_session_id      TEXT,
    stripe_payment_intent  TEXT,
    state                  TEXT NOT NULL,
    tracking_url           TEXT,
    order_id               TEXT,
    error_message          TEXT,
    yonder_total           REAL,
    charged_cents          INTEGER,
    created_at             REAL NOT NULL,
    updated_at             REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_state_idx ON orders(state);
CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(discord_user_id);
"""


@dataclass
class StoredOrder:
    checkout_id: str
    discord_user_id: int
    cart_code: str
    address: Optional[str]
    tip: Optional[float]
    stripe_session_id: Optional[str]
    stripe_payment_intent: Optional[str]
    state: str
    tracking_url: Optional[str]
    order_id: Optional[str]
    error_message: Optional[str]
    yonder_total: Optional[float]
    charged_cents: Optional[int]
    created_at: float
    updated_at: float


class OrderStore:
    def __init__(self, path: str = "slashdat.db"):
        self._path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        log.info("storage: db at %s ready", self._path)

    async def insert(
        self,
        *,
        checkout_id: str,
        discord_user_id: int,
        cart_code: str,
        address: Optional[str],
        tip: Optional[float],
        stripe_session_id: Optional[str],
        state: str,
        yonder_total: Optional[float],
        charged_cents: Optional[int],
    ) -> None:
        now = time.time()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO orders (
                    checkout_id, discord_user_id, cart_code, address, tip,
                    stripe_session_id, state, yonder_total, charged_cents,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkout_id) DO NOTHING
                """,
                (
                    checkout_id, discord_user_id, cart_code, address, tip,
                    stripe_session_id, state, yonder_total, charged_cents,
                    now, now,
                ),
            )
            await db.commit()

    async def update(self, checkout_id: str, **fields) -> None:
        if not fields:
            return
        allowed = {
            "stripe_session_id",
            "stripe_payment_intent",
            "state",
            "tracking_url",
            "order_id",
            "error_message",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"OrderStore.update got unsupported fields: {sorted(bad)}")
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [checkout_id]
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                f"UPDATE orders SET {set_clause} WHERE checkout_id = ?",
                values,
            )
            await db.commit()

    async def get(self, checkout_id: str) -> Optional[StoredOrder]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM orders WHERE checkout_id = ?", (checkout_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return _row_to_order(row) if row else None

    async def list_live(self) -> list[StoredOrder]:
        """Load every order still in a non-terminal state. For startup resume."""
        placeholders = ",".join("?" for _ in LIVE_STATES)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY created_at",
                LIVE_STATES,
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_order(r) for r in rows]

    async def list_for_user(self, discord_user_id: int, limit: int = 20) -> list[StoredOrder]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM orders WHERE discord_user_id = ? ORDER BY created_at DESC LIMIT ?",
                (discord_user_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_order(r) for r in rows]


def _row_to_order(row: aiosqlite.Row) -> StoredOrder:
    return StoredOrder(
        checkout_id=row["checkout_id"],
        discord_user_id=row["discord_user_id"],
        cart_code=row["cart_code"],
        address=row["address"],
        tip=row["tip"],
        stripe_session_id=row["stripe_session_id"],
        stripe_payment_intent=row["stripe_payment_intent"],
        state=row["state"],
        tracking_url=row["tracking_url"],
        order_id=row["order_id"],
        error_message=row["error_message"],
        yonder_total=row["yonder_total"],
        charged_cents=row["charged_cents"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
