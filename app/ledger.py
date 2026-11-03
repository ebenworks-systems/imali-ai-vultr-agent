"""Double-entry books: the chart of accounts, posting an entry and reading balances.

Every entry is checked twice. `check_lines` refuses an entry that does not balance before it
is written, and the database's deferred trigger (migration 001) refuses one at commit if any
code path ever gets past the first check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

import psycopg

# code, name, kind, hint (the hint is what the ranker reads when it suggests an account)
CHART: tuple[tuple[str, str, str, str], ...] = (
    ("1000", "Business bank account", "asset", "money in the bank account"),
    ("1010", "Cash on hand", "asset", "notes and coins in the till before they are banked"),
    ("1100", "Trade debtors", "asset", "contractors who bought on a 30-day account and still owe"),
    ("1150", "Refunds due to us", "asset", "money a supplier or the bank must pay back, such as a double payment"),
    ("1200", "Stock", "asset", "building materials held for sale"),
    ("2000", "Trade creditors", "liability", "suppliers we owe for stock bought on account"),
    ("2100", "Wages owed", "liability", "staff pay not yet paid out"),
    ("3000", "Owner's capital", "equity", "money the owner put into the business"),
    ("3100", "Owner's drawings", "equity", "money the owner took out for personal use"),
    ("4000", "Sales", "income", "counter sales, card sales and sales on account"),
    ("4100", "Delivery fees charged", "income", "delivery charged to customers"),
    ("5000", "Stock purchases", "expense", "cement, bricks, timber, steel and other stock bought for resale"),
    ("6000", "Wages", "expense", "salaries and wages for yard and counter staff"),
    ("6100", "Rent", "expense", "rent for the yard and the shop"),
    ("6200", "Electricity and water", "expense", "prepaid electricity, water and municipal charges"),
    ("6300", "Fuel and vehicle costs", "expense", "diesel and petrol for the delivery truck, vehicle repairs"),
    ("6400", "Bank charges", "expense", "monthly account fee, transaction and cash deposit fees"),
    ("6500", "Phone and internet", "expense", "airtime, mobile data and the shop's internet line"),
    ("6600", "Repairs and maintenance", "expense", "repairs to the yard, the building and equipment"),
    ("6700", "Security", "expense", "alarm monitoring and guarding of the yard"),
    ("6900", "Other expenses", "expense", "small costs that fit nowhere else"),
)

KINDS = {code: kind for code, _n, kind, _h in CHART}
NAMES = {code: name for code, name, _k, _h in CHART}
BANK = "1000"
CASH = "1010"
DEBTORS = "1100"
REFUNDS = "1150"
CREDITORS = "2000"
SALES = "4000"
PURCHASES = "5000"
EXPENSE_CODES = tuple(code for code, _n, kind, _h in CHART if kind == "expense")


class LedgerError(ValueError):
    """An entry the books will not take."""


@dataclass(frozen=True)
class Line:
    account: str
    debit: int = 0
    credit: int = 0
    document_id: int | None = None


def check_lines(lines: Sequence[Line], accounts: Iterable[str] | None = None) -> None:
    """Refuse an entry unless it has two or more lines, each a debit or a credit of whole
    positive cents on a known account, and its debits equal its credits."""
    known = set(accounts) if accounts is not None else set(KINDS)
    if len(lines) < 2:
        raise LedgerError("an entry needs at least two lines")
    total_dr = total_cr = 0
    for ln in lines:
        for v in (ln.debit, ln.credit):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise LedgerError("amounts are whole positive cents")
        if (ln.debit > 0) == (ln.credit > 0):
            raise LedgerError("each line is either a debit or a credit")
        if ln.account not in known:
            raise LedgerError("unknown account")
        total_dr += ln.debit
        total_cr += ln.credit
    if total_dr != total_cr:
        raise LedgerError("debits and credits differ")


def signed_balance(kind: str, debit: int, credit: int) -> int:
    """An account's balance on its normal side: debit-normal for assets and expenses."""
    if kind in ("asset", "expense"):
        return debit - credit
    return credit - debit


def post(
    conn: psycopg.Connection,
    workspace_id: str,
    entry_date: date,
    memo: str,
    lines: Sequence[Line],
    *,
    source: str,
    proposal_id: int | None = None,
) -> int:
    """Write one balanced entry inside the caller's transaction and return its id."""
    check_lines(lines)
    row = conn.execute(
        "INSERT INTO journal_entries (workspace_id, entry_date, memo, source, proposal_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (workspace_id, entry_date, memo[:200], source, proposal_id),
    ).fetchone()
    entry_id = row["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO journal_lines (entry_id, workspace_id, account_code, debit_cents, credit_cents, document_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [(entry_id, workspace_id, ln.account, ln.debit, ln.credit, ln.document_id) for ln in lines],
        )
    return entry_id


def trial_balance(conn: psycopg.Connection, workspace_id: str, upto: date | None = None) -> list[dict]:
    """Each account's debit and credit totals and its balance, in chart order."""
    rows = conn.execute(
        """
        SELECT a.code, a.name, a.kind,
               coalesce(sum(l.debit_cents), 0)::bigint AS debit,
               coalesce(sum(l.credit_cents), 0)::bigint AS credit
          FROM accounts a
          LEFT JOIN (journal_lines l
                     JOIN journal_entries e ON e.id = l.entry_id
                      AND (%s::date IS NULL OR e.entry_date <= %s::date))
                 ON l.workspace_id = a.workspace_id AND l.account_code = a.code
         WHERE a.workspace_id = %s
         GROUP BY a.code, a.name, a.kind
         ORDER BY a.code
        """,
        (upto, upto, workspace_id),
    ).fetchall()
    out = []
    for r in rows:
        out.append({**r, "balance": signed_balance(r["kind"], r["debit"], r["credit"])})
    return out


def totals(tb: Sequence[dict]) -> tuple[int, int]:
    return sum(r["debit"] for r in tb), sum(r["credit"] for r in tb)


def balance_of(conn: psycopg.Connection, workspace_id: str, code: str, upto: date | None = None) -> int:
    r = conn.execute(
        """
        SELECT coalesce(sum(l.debit_cents), 0)::bigint AS d, coalesce(sum(l.credit_cents), 0)::bigint AS c
          FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id
         WHERE l.workspace_id = %s AND l.account_code = %s
           AND (%s::date IS NULL OR e.entry_date <= %s::date)
        """,
        (workspace_id, code, upto, upto),
    ).fetchone()
    return signed_balance(KINDS.get(code, "asset"), r["d"], r["c"])


def period_activity(conn: psycopg.Connection, workspace_id: str, start: date, end: date) -> dict[str, int]:
    """Net movement of each account inside a period, on its normal side."""
    rows = conn.execute(
        """
        SELECT l.account_code AS code,
               coalesce(sum(l.debit_cents), 0)::bigint AS d, coalesce(sum(l.credit_cents), 0)::bigint AS c
          FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id
         WHERE l.workspace_id = %s AND e.entry_date BETWEEN %s AND %s AND e.source <> 'opening'
         GROUP BY l.account_code
        """,
        (workspace_id, start, end),
    ).fetchall()
    return {r["code"]: signed_balance(KINDS.get(r["code"], "asset"), r["d"], r["c"]) for r in rows}
