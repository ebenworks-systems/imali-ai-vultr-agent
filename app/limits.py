"""Durable limits kept in Postgres, so they hold across restarts and across the web app and the
worker. A bucket names what is counted and whose it is; an address appears only as a keyed hash.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

import psycopg

from .config import settings


class LimitReached(Exception):
    """A limit was reached. `message` is the sentence the visitor sees."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def addr_hash(addr: str) -> str:
    key = (settings().secret_key or "unset").encode()
    return hmac.new(key, addr.encode(), hashlib.sha256).hexdigest()[:32]


# A copy's own limits run for the copy's whole life, so their window never rolls over; the
# worker removes them once the copy is gone.
LIFETIME = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _window(seconds: int, now: datetime | None = None) -> datetime:
    if seconds == COPY:
        return LIFETIME
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, timezone.utc)


def hit(conn: psycopg.Connection, bucket: str, limit: int, seconds: int, *, cost: int = 1) -> int:
    """Count `cost` against the bucket's current window and return the new count, or raise
    LimitReached (counting nothing) when that would pass the limit."""
    start = _window(seconds)
    row = conn.execute(
        """
        INSERT INTO rate_counters (bucket, window_start, n) VALUES (%s, %s, %s)
        ON CONFLICT (bucket, window_start) DO UPDATE SET n = rate_counters.n + EXCLUDED.n
        WHERE rate_counters.n + EXCLUDED.n <= %s
        RETURNING n
        """,
        (bucket, start, cost, limit),
    ).fetchone()
    if row is None or row["n"] > limit:
        raise LimitReached("")
    return row["n"]


def check(conn: psycopg.Connection, rules: list[tuple[str, int, int]], message: str, *, cost: int = 1) -> None:
    """Apply several limits as one: all count, or none does and the visitor gets `message`."""
    conn.execute("SAVEPOINT limits")
    try:
        for bucket, limit, seconds in rules:
            hit(conn, bucket, limit, seconds, cost=cost)
    except LimitReached:
        conn.execute("ROLLBACK TO SAVEPOINT limits")
        raise LimitReached(message) from None
    conn.execute("RELEASE SAVEPOINT limits")


DAY = 86_400
HOUR = 3_600
COPY = 2 * HOUR + 600  # a little longer than a copy lives


def model_call(conn: psycopg.Connection, service: str, workspace_id: str | None = None) -> None:
    """Every call to a model counts against the site's daily ceiling, whichever model it is, and
    against the copy's and its visitor's address's own shares."""
    lim = settings().limits
    rules = [("model:site", lim.model_calls_site_per_day, DAY)]
    if workspace_id:
        rules.append((f"model:ws:{workspace_id}", lim.model_calls_per_copy, COPY))
        w = conn.execute("SELECT addr_hash FROM workspaces WHERE id = %s", (workspace_id,)).fetchone()
        if w is None:
            # The copy was removed (Start over) while its work ran: stop that work here, so
            # calls made for a removed copy never escape its address's share.
            raise LimitReached("This copy has ended. Start over for a fresh copy.")
        rules.append((f"model:addr:{w['addr_hash']}", lim.model_calls_addr_per_day, DAY))
    check(conn, rules, "The agent has done all the work it can do today. Please come back tomorrow.")


def prune(conn: psycopg.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM rate_counters WHERE window_start < now() - interval '2 days' AND window_start <> %s",
        (LIFETIME,),
    )
    gone = conn.execute(
        "DELETE FROM rate_counters r WHERE r.window_start = %s AND NOT EXISTS "
        "(SELECT 1 FROM workspaces w WHERE r.bucket LIKE '%%:ws:' || w.id::text)",
        (LIFETIME,),
    )
    return cur.rowcount + gone.rowcount
