from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs the throwaway Postgres from scripts/testdb.sh")


def pytest_collection_modifyitems(config, items):
    if TEST_DB:
        return
    if os.environ.get("REQUIRE_DB"):
        raise pytest.UsageError("REQUIRE_DB is set but TEST_DATABASE_URL is not: run `make test`")
    skip = pytest.mark.skip(reason="no TEST_DATABASE_URL; run `make test` for the database tests")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def db_url():
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-the-suite-only")
    from app import config, db

    config.reset_settings()
    db.close_pool()
    db.migrate()
    yield TEST_DB
    db.close_pool()


@pytest.fixture()
def conn(db_url):
    with psycopg.connect(db_url, row_factory=dict_row) as c:
        yield c


@pytest.fixture()
def workspace(db_url):
    """A fresh seeded copy of the books, removed after the test."""
    from app import db, seed

    wid = str(uuid.uuid4())
    with db.tx() as c:
        c.execute(
            "INSERT INTO workspaces (id, expires_at, addr_hash, shop_name, period_start, period_end) "
            "VALUES (%s, %s, 'test', %s, %s, %s)",
            (wid, datetime.now(timezone.utc) + timedelta(hours=2), seed.SHOP, seed.PERIOD_START, seed.PERIOD_END),
        )
        seed.seed(c, wid)
    yield wid
    with db.tx() as c:
        c.execute("DELETE FROM workspaces WHERE id = %s", (wid,))
