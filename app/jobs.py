"""Queueing the month-end jobs. The worker (app/worker.py) runs them; the web app only queues."""

from __future__ import annotations

import psycopg

from . import close_job, limits, seed
from .config import settings
from .sandbox import read_statement


def statement_on_file(conn: psycopg.Connection, wid: str) -> int:
    """The copy's latest statement, or October's from the bank feed when none was uploaded."""
    r = conn.execute(
        "SELECT id FROM statements WHERE workspace_id = %s ORDER BY id DESC LIMIT 1", (wid,)
    ).fetchone()
    if r:
        return r["id"]
    st = read_statement(seed.statement_csv())
    return close_job.store_statement(conn, wid, "statement-october-2026.csv", st)


def start_close(conn: psycopg.Connection, wid: str, addr: str | None) -> tuple[int, bool]:
    """Queue a close for the statement on file, or return the close already made for it."""
    # Two requests at once (the chat and the button, or a quick retry) wait here for each other,
    # so the second finds the first one's statement and job instead of making its own.
    conn.execute("SELECT 1 FROM workspaces WHERE id = %s FOR UPDATE", (wid,))
    sid = statement_on_file(conn, wid)
    existing = conn.execute(
        "SELECT id FROM jobs WHERE workspace_id = %s AND kind = 'close_month' AND statement_id = %s "
        "AND status <> 'failed' ORDER BY id DESC LIMIT 1",
        (wid, sid),
    ).fetchone()
    if existing:
        return existing["id"], True
    lim = settings().limits
    rules = [(f"close:ws:{wid}", lim.closes_per_copy, limits.COPY)]
    if addr:
        rules.append((f"close:day:{limits.addr_hash(addr)}", lim.closes_per_day, limits.DAY))
    limits.check(conn, rules, "This copy has closed the month as many times as it can. Start over for a fresh copy.")
    r = conn.execute(
        "INSERT INTO jobs (workspace_id, kind, statement_id) VALUES (%s, 'close_month', %s) RETURNING id", (wid, sid)
    ).fetchone()
    conn.execute("NOTIFY month_end_jobs")
    return r["id"], False


def start_post(conn: psycopg.Connection, wid: str, close_id: int) -> int:
    parent = conn.execute(
        "SELECT * FROM jobs WHERE id = %s AND workspace_id = %s AND kind = 'close_month' FOR UPDATE", (close_id, wid)
    ).fetchone()
    if parent is None or parent["status"] != "waiting":
        raise ValueError("That close is not waiting for you.")
    running = conn.execute(
        "SELECT id FROM jobs WHERE parent_id = %s AND status IN ('queued', 'running')", (close_id,)
    ).fetchone()
    if running:
        return running["id"]
    r = conn.execute(
        "INSERT INTO jobs (workspace_id, kind, parent_id) VALUES (%s, 'post_approved', %s) RETURNING id", (wid, close_id)
    ).fetchone()
    conn.execute("NOTIFY month_end_jobs")
    return r["id"]


def claim_next(conn: psycopg.Connection) -> dict | None:
    """Take the oldest queued job; another worker skips it."""
    r = conn.execute(
        "SELECT id, kind, workspace_id FROM jobs WHERE status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
    ).fetchone()
    if r is None:
        return None
    conn.execute("UPDATE jobs SET status = 'running', started_at = now() WHERE id = %s", (r["id"],))
    return r
