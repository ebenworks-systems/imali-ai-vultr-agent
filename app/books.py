"""Reading the books: open documents, who owes what, the statement, the journal."""

from __future__ import annotations

from datetime import date

import psycopg

from . import ledger, matching


def open_documents(conn: psycopg.Connection, wid: str) -> list[matching.OpenDoc]:
    rows = conn.execute(
        """
        SELECT d.id, d.kind, d.number, coalesce(p.name, '') AS party, d.issued_on, d.due_on,
               (d.total_cents - d.paid_cents)::bigint AS open_cents
          FROM documents d LEFT JOIN parties p ON p.id = d.party_id
         WHERE d.workspace_id = %s AND d.total_cents > d.paid_cents
         ORDER BY d.issued_on, d.id
        """,
        (wid,),
    ).fetchall()
    return [
        matching.OpenDoc(r["id"], r["kind"], r["number"], r["party"], r["issued_on"], r["due_on"], r["open_cents"])
        for r in rows
    ]


def parties(conn: psycopg.Connection, wid: str, kind: str) -> list[dict]:
    return conn.execute(
        "SELECT id, name, contact, terms_days FROM parties WHERE workspace_id = %s AND kind = %s ORDER BY name",
        (wid, kind),
    ).fetchall()


def accounts(conn: psycopg.Connection, wid: str) -> list[matching.Account]:
    rows = conn.execute(
        "SELECT code, name, kind, hint FROM accounts WHERE workspace_id = %s ORDER BY code", (wid,)
    ).fetchall()
    return [matching.Account(r["code"], r["name"], r["kind"], r["hint"]) for r in rows]


BUCKETS = (("Not yet due", None, 0), ("1 to 30 days late", 1, 30), ("31 to 60 days late", 31, 60), ("More than 60 days late", 61, None))


def bucket_of(days_late: int) -> str:
    if days_late <= 0:
        return BUCKETS[0][0]
    if days_late <= 30:
        return BUCKETS[1][0]
    if days_late <= 60:
        return BUCKETS[2][0]
    return BUCKETS[3][0]


def who_owes(conn: psycopg.Connection, wid: str, as_at: date) -> list[dict]:
    """Each customer with open invoices, oldest first, with days late on the given day."""
    rows = conn.execute(
        """
        SELECT p.id AS party_id, p.name, p.contact, d.id AS document_id, d.number, d.issued_on, d.due_on,
               d.total_cents, (d.total_cents - d.paid_cents)::bigint AS open_cents
          FROM documents d JOIN parties p ON p.id = d.party_id
         WHERE d.workspace_id = %s AND d.kind = 'invoice' AND d.total_cents > d.paid_cents
         ORDER BY p.name, d.issued_on
        """,
        (wid,),
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        late = (as_at - r["due_on"]).days
        c = out.setdefault(
            r["party_id"],
            {"party_id": r["party_id"], "name": r["name"], "contact": r["contact"], "open_cents": 0,
             "late_cents": 0, "oldest_days_late": 0, "invoices": []},
        )
        c["open_cents"] += r["open_cents"]
        if late > 0:
            c["late_cents"] += r["open_cents"]
        c["oldest_days_late"] = max(c["oldest_days_late"], late)
        c["invoices"].append({
            "document_id": r["document_id"], "number": r["number"], "issued_on": r["issued_on"].isoformat(),
            "due_on": r["due_on"].isoformat(), "total_cents": r["total_cents"], "open_cents": r["open_cents"],
            "days_late": late, "bucket": bucket_of(late),
        })
    return sorted(out.values(), key=lambda c: (-c["late_cents"], -c["open_cents"]))


def ageing(debtors: list[dict]) -> list[dict]:
    totals = {name: 0 for name, _a, _b in BUCKETS}
    for c in debtors:
        for inv in c["invoices"]:
            totals[inv["bucket"]] += inv["open_cents"]
    return [{"bucket": k, "cents": v} for k, v in totals.items()]


def statement_lines(conn: psycopg.Connection, wid: str, statement_id: int) -> list[matching.StLine]:
    rows = conn.execute(
        "SELECT id, line_date, description, reference, amount_cents FROM statement_lines "
        "WHERE statement_id = %s AND workspace_id = %s ORDER BY line_date, line_no",
        (statement_id, wid),
    ).fetchall()
    return [matching.StLine(r["id"], r["line_date"], r["description"], r["reference"], r["amount_cents"]) for r in rows]


def journal(conn: psycopg.Connection, wid: str, *, offset: int = 0, limit: int = 30, source: str | None = None,
            order: str = "newest") -> tuple[list[dict], int]:
    where = "e.workspace_id = %s"
    args: list = [wid]
    if source:
        where += " AND e.source = %s"
        args.append(source)
    total = conn.execute(f"SELECT count(*) AS n FROM journal_entries e WHERE {where}", args).fetchone()["n"]
    direction = "DESC" if order == "newest" else "ASC"
    rows = conn.execute(
        f"""
        SELECT e.id, e.entry_date, e.memo, e.source, e.posted_at,
               json_agg(json_build_object('account', l.account_code, 'name', a.name,
                        'debit', l.debit_cents, 'credit', l.credit_cents) ORDER BY l.id) AS lines
          FROM journal_entries e
          JOIN journal_lines l ON l.entry_id = e.id
          JOIN accounts a ON a.workspace_id = l.workspace_id AND a.code = l.account_code
         WHERE {where}
         GROUP BY e.id
         ORDER BY e.entry_date {direction}, e.id {direction}
         OFFSET %s LIMIT %s
        """,
        args + [offset, limit],
    ).fetchall()
    return rows, total


def bank_reconciliation(conn: psycopg.Connection, wid: str, statement: dict) -> dict:
    """The bank balance in the books on the statement's last day against the statement's own."""
    books = ledger.balance_of(conn, wid, ledger.BANK, statement["period_end"])
    pending = conn.execute(
        "SELECT coalesce(sum(amount_cents), 0)::bigint AS s, count(*) AS n FROM statement_lines "
        "WHERE statement_id = %s AND status <> 'posted'",
        (statement["id"],),
    ).fetchone()
    closing = statement["closing_cents"]
    return {
        "statement_cents": closing,
        "books_cents": books,
        "unposted_cents": pending["s"],
        "unposted_lines": pending["n"],
        "difference_cents": (closing - books - pending["s"]) if closing is not None else None,
    }
