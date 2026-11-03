"""The Postgres pool and the migrations runner."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

MIGRATIONS = Path(__file__).parent / "migrations"

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = settings().database_url
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """One transaction: commits when the block ends, rolls back if it raises."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def migrate(conn: psycopg.Connection | None = None) -> list[int]:
    """Apply every migration file not yet recorded. Safe to run on each start."""

    def run(c: psycopg.Connection) -> list[int]:
        applied: list[int] = []
        c.execute("SELECT pg_advisory_xact_lock(720_043_001)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in c.execute("SELECT version FROM schema_version").fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in done:
                continue
            c.execute(path.read_text())
            c.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
            applied.append(version)
        return applied

    if conn is not None:
        with conn.transaction():
            return run(conn)
    with tx() as c:
        return run(c)
