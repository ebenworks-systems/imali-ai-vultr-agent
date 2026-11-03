"""The ledger balance tests: every entry balances, the seeded books balance, and the database
itself refuses an entry that does not."""

from datetime import date

import psycopg
import pytest

from app import ledger, seed
from app.ledger import Line, LedgerError, check_lines


def test_check_lines_takes_a_balanced_entry():
    check_lines([Line("1000", debit=500), Line("4000", credit=500)])
    check_lines([Line("1000", debit=500), Line("1100", credit=300), Line("1150", credit=200)])


@pytest.mark.parametrize(
    "lines",
    [
        [Line("1000", debit=500)],
        [Line("1000", debit=500), Line("4000", credit=499)],
        [Line("1000", debit=500, credit=500), Line("4000", credit=0)],
        [Line("1000", debit=-5), Line("4000", credit=-5)],
        [Line("1000", debit=5.0), Line("4000", credit=5.0)],
        [Line("9999", debit=5), Line("4000", credit=5)],
        [Line("1000", debit=True), Line("4000", credit=True)],
    ],
)
def test_check_lines_refuses_bad_entries(lines):
    with pytest.raises(LedgerError):
        check_lines(lines)


def test_signed_balance_follows_the_normal_side():
    assert ledger.signed_balance("asset", 900, 100) == 800
    assert ledger.signed_balance("expense", 900, 100) == 800
    assert ledger.signed_balance("income", 100, 900) == 800
    assert ledger.signed_balance("liability", 100, 900) == 800


@pytest.mark.db
def test_the_seeded_books_balance_and_match_their_documents(workspace, conn):
    tb = ledger.trial_balance(conn, workspace)
    debit, credit = ledger.totals(tb)
    assert debit == credit > 0
    by_code = {r["code"]: r for r in tb}
    assert by_code["1000"]["balance"] == seed.OPENING_BANK
    # The debtors and creditors control accounts equal the open documents behind them.
    docs = conn.execute(
        "SELECT kind, sum(total_cents - paid_cents)::bigint AS open FROM documents "
        "WHERE workspace_id = %s GROUP BY kind",
        (workspace,),
    ).fetchall()
    open_by_kind = {r["kind"]: r["open"] for r in docs}
    assert by_code["1100"]["balance"] == open_by_kind["invoice"]
    assert by_code["2000"]["balance"] == open_by_kind["bill"]
    assert by_code["1010"]["balance"] == seed.OPENING_CASH + sum(c for _n, _d, c in seed.CASHUPS)


@pytest.mark.db
def test_the_database_refuses_an_unbalanced_entry_at_commit(workspace, db_url):
    with psycopg.connect(db_url) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            with c.transaction():
                eid = c.execute(
                    "INSERT INTO journal_entries (workspace_id, entry_date, memo, source) "
                    "VALUES (%s, '2026-10-31', 'bad', 'books') RETURNING id",
                    (workspace,),
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO journal_lines (entry_id, workspace_id, account_code, debit_cents) VALUES (%s, %s, '1000', 100)",
                    (eid, workspace),
                )
                c.execute(
                    "INSERT INTO journal_lines (entry_id, workspace_id, account_code, credit_cents) VALUES (%s, %s, '4000', 99)",
                    (eid, workspace),
                )


@pytest.mark.db
def test_the_database_refuses_an_entry_with_no_lines(workspace, db_url):
    with psycopg.connect(db_url) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            with c.transaction():
                c.execute(
                    "INSERT INTO journal_entries (workspace_id, entry_date, memo, source) VALUES (%s, '2026-10-31', 'empty', 'books')",
                    (workspace,),
                )


@pytest.mark.db
def test_a_line_cannot_be_both_sides_or_a_fraction(workspace, db_url):
    with psycopg.connect(db_url) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            with c.transaction():
                eid = c.execute(
                    "INSERT INTO journal_entries (workspace_id, entry_date, memo, source) VALUES (%s, '2026-10-31', 'x', 'books') RETURNING id",
                    (workspace,),
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO journal_lines (entry_id, workspace_id, account_code, debit_cents, credit_cents) "
                    "VALUES (%s, %s, '1000', 100, 100)",
                    (eid, workspace),
                )


@pytest.mark.db
def test_posting_through_the_code_keeps_the_trial_balance(workspace, db_url):
    from app import db

    with db.tx() as c:
        ledger.post(c, workspace, date(2026, 10, 31), "Rent", [Line("6100", debit=1_850_000), Line("1000", credit=1_850_000)], source="close")
    with db.tx() as c:
        d, cr = ledger.totals(ledger.trial_balance(c, workspace))
        assert d == cr
        assert ledger.balance_of(c, workspace, "1000") == seed.OPENING_BANK - 1_850_000


@pytest.mark.db
def test_removing_a_copy_removes_all_of_it(db_url):
    import uuid
    from datetime import datetime, timedelta, timezone

    from app import db

    wid = str(uuid.uuid4())
    with db.tx() as c:
        c.execute(
            "INSERT INTO workspaces (id, expires_at, addr_hash, shop_name, period_start, period_end) VALUES (%s, %s, 't', 's', %s, %s)",
            (wid, datetime.now(timezone.utc) + timedelta(hours=2), seed.PERIOD_START, seed.PERIOD_END),
        )
        seed.seed(c, wid)
    with db.tx() as c:
        c.execute("DELETE FROM workspaces WHERE id = %s", (wid,))
    with db.tx() as c:
        for table in ("accounts", "parties", "documents", "journal_entries", "journal_lines", "audit_log"):
            n = c.execute(f"SELECT count(*) AS n FROM {table} WHERE workspace_id = %s", (wid,)).fetchone()["n"]
            assert n == 0, table
