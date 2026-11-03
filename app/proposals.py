"""Everything the agent wants to change waits as a proposal until the owner approves or rejects
it. Approving a record from the chat posts it at once; approving a month-end item marks it for
step 5, which posts every approved item together. Each decision lands in the audit log."""

from __future__ import annotations

from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from . import ledger
from .ledger import Line, LedgerError
from .workspace import audit


class ProposalError(ValueError):
    """A decision the books cannot take; the message is safe to show."""


def create(conn: psycopg.Connection, wid: str, kind: str, title: str, detail: str, payload: dict,
           job_id: int | None = None) -> int:
    r = conn.execute(
        "INSERT INTO proposals (workspace_id, job_id, kind, title, detail, payload) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (wid, job_id, kind, title[:160], detail[:900], Jsonb(payload)),
    ).fetchone()
    audit(conn, wid, "agent", f"proposed_{kind}", "proposal", r["id"], {"title": title[:160]})
    return r["id"]


def _lock(conn: psycopg.Connection, wid: str, pid: int) -> dict:
    p = conn.execute(
        "SELECT * FROM proposals WHERE id = %s AND workspace_id = %s FOR UPDATE", (pid, wid)
    ).fetchone()
    if p is None:
        raise ProposalError("That item is no longer here.")
    return p


def _entry_for_choice(p: dict, account: str, accounts: set[str]) -> list[dict]:
    """A flagged bank line with no entry gets one from the account the owner picked."""
    if account not in accounts or account in (ledger.BANK,):
        raise ProposalError("Pick one of the listed accounts.")
    amt = int(p["payload"].get("amount_cents") or 0)
    if amt == 0:
        raise ProposalError("This item has no amount to post.")
    if amt > 0:
        return [{"account": ledger.BANK, "debit": amt, "credit": 0, "document_id": None},
                {"account": account, "debit": 0, "credit": amt, "document_id": None}]
    return [{"account": account, "debit": -amt, "credit": 0, "document_id": None},
            {"account": ledger.BANK, "debit": 0, "credit": -amt, "document_id": None}]


def approve(conn: psycopg.Connection, wid: str, pid: int, *, account: str | None = None) -> dict:
    p = _lock(conn, wid, pid)
    if p["status"] != "pending":
        raise ProposalError("That item has already been decided.")
    payload = dict(p["payload"])
    if p["kind"] in ("match", "flag"):
        known = {r["code"] for r in conn.execute("SELECT code FROM accounts WHERE workspace_id = %s", (wid,)).fetchall()}
        if account:
            if account not in set(payload.get("choices") or []) | {c for c in known if c != ledger.BANK}:
                raise ProposalError("Pick one of the listed accounts.")
            if payload.get("applies"):
                raise ProposalError("This payment is tied to an invoice or bill, so its account cannot change.")
            payload["entry"] = _entry_for_choice(p, account, known)
            payload["chosen_account"] = account
        if not payload.get("entry"):
            raise ProposalError("Pick the account this belongs to first, or set it aside.")
    conn.execute(
        "UPDATE proposals SET status = 'approved', decided_at = now(), payload = %s WHERE id = %s",
        (Jsonb(payload), pid),
    )
    audit(conn, wid, "owner", "approved", "proposal", pid, {"kind": p["kind"], "account": account})
    if p["kind"] == "record":
        post_one(conn, wid, pid)
    return {"id": pid, "status": "posted" if p["kind"] == "record" else "approved"}


def reject(conn: psycopg.Connection, wid: str, pid: int, reason: str = "") -> dict:
    p = _lock(conn, wid, pid)
    if p["status"] != "pending":
        raise ProposalError("That item has already been decided.")
    conn.execute("UPDATE proposals SET status = 'rejected', decided_at = now() WHERE id = %s", (pid,))
    line_id = (p["payload"] or {}).get("line_id")
    if line_id:
        conn.execute(
            "UPDATE statement_lines SET status = 'set_aside' WHERE id = %s AND workspace_id = %s", (line_id, wid)
        )
    audit(conn, wid, "owner", "rejected", "proposal", pid, {"kind": p["kind"], "reason": reason[:200]})
    return {"id": pid, "status": "rejected"}


def post_one(conn: psycopg.Connection, wid: str, pid: int) -> int:
    """Post one approved proposal in its own savepoint and return the journal entry id."""
    p = _lock(conn, wid, pid)
    if p["status"] != "approved":
        raise ProposalError("Only approved items are posted.")
    payload = p["payload"]
    conn.execute("SAVEPOINT post_one")
    try:
        entry_date = date.fromisoformat(payload["entry_date"])
        new_inv = payload.get("new_invoice")
        lines_in = list(payload["entry"])
        if new_inv:
            r = conn.execute(
                "INSERT INTO documents (workspace_id, kind, number, party_id, issued_on, due_on, total_cents, memo) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (wid, "bill" if new_inv.get("kind") == "bill" else "invoice", new_inv["number"], new_inv["party_id"],
                 entry_date, new_inv["due_on"], new_inv["total_cents"], new_inv.get("memo", "")[:120]),
            ).fetchone()
            lines_in = [{**ln, "document_id": r["id"]} if ln.get("new_invoice") else ln for ln in lines_in]
        lines = [Line(ln["account"], int(ln["debit"]), int(ln["credit"]), ln.get("document_id")) for ln in lines_in]
        ledger.check_lines(lines)
        eid = ledger.post(conn, wid, entry_date, payload.get("memo") or p["title"], lines,
                          source="agent" if p["kind"] == "record" else "close", proposal_id=pid)
        for a in payload.get("applies") or []:
            cur = conn.execute(
                "UPDATE documents SET paid_cents = paid_cents + %s WHERE id = %s AND workspace_id = %s "
                "AND paid_cents + %s <= total_cents",
                (a["cents"], a["document_id"], wid, a["cents"]),
            )
            if cur.rowcount != 1:
                raise ProposalError(f"{a.get('number', 'That document')} no longer has that much open.")
        if payload.get("line_id"):
            conn.execute(
                "UPDATE statement_lines SET status = 'posted' WHERE id = %s AND workspace_id = %s",
                (payload["line_id"], wid),
            )
        conn.execute("UPDATE proposals SET status = 'posted', posted_at = now() WHERE id = %s", (pid,))
        audit(conn, wid, "system", "posted", "journal_entry", eid, {"proposal": pid})
    except (LedgerError, ProposalError, KeyError, ValueError, psycopg.errors.CheckViolation,
            psycopg.errors.UniqueViolation) as exc:
        conn.execute("ROLLBACK TO SAVEPOINT post_one")
        msg = exc.args[0] if isinstance(exc, ProposalError) else "That entry could not be posted."
        raise ProposalError(msg) from None
    conn.execute("RELEASE SAVEPOINT post_one")
    return eid
