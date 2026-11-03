"""Each visitor works in a private copy of the books that lives two hours. Opening one is
limited per address and across the site; "Start over" replaces it with a fresh copy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

from . import limits, seed
from .config import settings

COOKIE = "imali_copy"


def _sign(payload: str) -> str:
    key = (settings().secret_key or "").encode()
    if not key:
        raise RuntimeError("SECRET_KEY is not set")
    return base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()[:24]).decode().rstrip("=")


def make_token(workspace_id: str, expires_at: datetime) -> str:
    payload = f"{workspace_id}.{int(expires_at.timestamp())}"
    return f"{payload}.{_sign(payload)}"


def read_token(token: str | None) -> str | None:
    """The workspace id in a valid, unexpired token, or None."""
    if not token or token.count(".") != 2:
        return None
    wid, exp, sig = token.split(".")
    payload = f"{wid}.{exp}"
    try:
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        if int(exp) < time.time():
            return None
        uuid.UUID(wid)
    except (ValueError, RuntimeError):
        return None
    return wid


def open_copy(conn: psycopg.Connection, addr: str, *, replacing: str | None = None) -> tuple[str, datetime]:
    """Make a new seeded copy for this address, inside the caller's transaction."""
    lim = settings().limits
    h = limits.addr_hash(addr)
    rules = [
        (f"copy:hour:{h}", lim.copies_per_hour, limits.HOUR),
        (f"copy:day:{h}", lim.copies_per_day, limits.DAY),
        ("copy:site", lim.copies_site_per_day, limits.DAY),
    ]
    limits.check(conn, rules, "You have opened as many copies as one address can for now. Please try again later.")
    if replacing:
        conn.execute("DELETE FROM workspaces WHERE id = %s", (replacing,))
    open_now = conn.execute("SELECT count(*) AS n FROM workspaces WHERE expires_at > now()").fetchone()["n"]
    if open_now >= lim.copies_open:
        raise limits.LimitReached("Every copy is in use right now. Please try again in a few minutes.")
    wid = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings().copy_minutes)
    conn.execute(
        "INSERT INTO workspaces (id, expires_at, addr_hash, shop_name, period_start, period_end) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (wid, expires, h, seed.SHOP, seed.PERIOD_START, seed.PERIOD_END),
    )
    seed.seed(conn, wid)
    return wid, expires


def get(conn: psycopg.Connection, wid: str) -> dict | None:
    return conn.execute(
        "SELECT id::text AS id, shop_name, expires_at, period_start, period_end FROM workspaces "
        "WHERE id = %s AND expires_at > now()",
        (wid,),
    ).fetchone()


def remove_expired(conn: psycopg.Connection) -> int:
    cur = conn.execute("DELETE FROM workspaces WHERE expires_at < now()")
    return cur.rowcount


def audit(conn: psycopg.Connection, wid: str, actor: str, action: str, object_type: str = "",
          object_id: str | int = "", detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (workspace_id, actor, action, object_type, object_id, detail) VALUES (%s, %s, %s, %s, %s, %s)",
        (wid, actor, action, object_type, str(object_id), psycopg.types.json.Jsonb(detail or {})),
    )
