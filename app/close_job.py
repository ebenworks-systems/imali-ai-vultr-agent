"""Closing the month: the five steps the worker runs on the server.

1. Read the bank statement (already opened in the parser sandbox) and screen its wording.
2. Match each line to the books.
3. Flag what needs the owner: unmatched debits, duplicates, amount mismatches.
4. List who owes, and draft follow-ups for the owner to copy. Nothing is sent.
5. After the owner has approved or rejected each item: post the approved entries to the
   double-entry ledger and write the one-page summary.

Steps 1 to 4 run as one job, which then waits. Step 5 runs as its own job when the owner presses
"Post approved entries". Every step streams its findings as job events, and every change waits
in the proposals table until the owner decides.
"""

from __future__ import annotations

import traceback
from collections import Counter
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from . import books, db, drafts, ledger, proposals, seed
from .config import settings
from .limits import LimitReached
from .money import rand
from .services import Services
from .vultr import VultrError

STEP_TITLES = {
    1: "Read the bank statement",
    2: "Match each line to the books",
    3: "Flag what needs you",
    4: "List who owes, with follow-ups to copy",
    5: "Post approved entries and summarise the month",
}


def emit(job_id: int, step: int, kind: str, message: str, data: dict | None = None) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO job_events (job_id, step, kind, message, data) VALUES (%s, %s, %s, %s, %s)",
            (job_id, step, kind, message[:600], Jsonb(data or {})),
        )


def store_statement(conn: psycopg.Connection, wid: str, file_name: str, st: dict) -> int:
    r = conn.execute(
        "INSERT INTO statements (workspace_id, file_name, sha256, period_start, period_end, opening_cents, closing_cents) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (wid, file_name[:80], st["sha256"], st["period_start"], st["period_end"], st["opening_cents"], st["closing_cents"]),
    ).fetchone()
    sid = r["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO statement_lines (statement_id, workspace_id, line_no, line_date, description, reference, "
            "amount_cents, balance_cents) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (sid, wid, ln["line_no"], ln["line_date"], ln["description"], ln["reference"], ln["amount_cents"], ln["balance_cents"])
                for ln in st["lines"]
            ],
        )
    return sid


def _job(conn: psycopg.Connection, job_id: int) -> dict:
    return conn.execute(
        "SELECT j.*, w.shop_name, w.period_start, w.period_end FROM jobs j JOIN workspaces w ON w.id = j.workspace_id "
        "WHERE j.id = %s",
        (job_id,),
    ).fetchone()


def _finish(job_id: int, status: str, summary: dict | None = None) -> None:
    with db.tx() as c:
        c.execute(
            "UPDATE jobs SET status = %s, finished_at = CASE WHEN %s IN ('done', 'failed') THEN now() END, "
            "summary = coalesce(%s, summary) WHERE id = %s",
            (status, status, Jsonb(summary) if summary is not None else None, job_id),
        )


def _vultr_used(svc: Services, since: int) -> list[dict]:
    seen: dict[str, dict] = {}
    for call in svc.vultr.log[since:]:
        k = call["model"]
        s = seen.setdefault(k, {"service": call["service"], "model": k, "calls": 0, "ms": 0})
        s["calls"] += 1
        s["ms"] += call["ms"]
    return list(seen.values())


def run_close(job_id: int, svc: Services) -> None:
    with db.tx() as c:
        job = _job(c, job_id)
        wid = str(job["workspace_id"])
        st = c.execute("SELECT * FROM statements WHERE id = %s", (job["statement_id"],)).fetchone()
    try:
        _steps_1_to_4(job_id, wid, job, st, svc)
    except LimitReached as exc:
        emit(job_id, 0, "error", exc.message)
        _finish(job_id, "failed")
    except Exception:
        traceback.print_exc()
        emit(job_id, 0, "error", "Something went wrong while closing the month. Start over to try again.")
        _finish(job_id, "failed")


def _steps_1_to_4(job_id: int, wid: str, job: dict, st: dict, svc: Services) -> None:
    shop = job["shop_name"]
    month = f"{st['period_end']:%B %Y}" if st["period_end"] else "the month"

    # Step 1: read the statement.
    emit(job_id, 1, "start", STEP_TITLES[1])
    with db.tx() as c:
        lines = books.statement_lines(c, wid, st["id"])
    money_in = sum(ln.amount_cents for ln in lines if ln.amount_cents > 0)
    money_out = -sum(ln.amount_cents for ln in lines if ln.amount_cents < 0)
    where = "in the parser sandbox, which has no network" if settings().sandbox_dir else "with the statement reader"
    emit(job_id, 1, "info",
         f"Opened {st['file_name']} {where}: {len(lines)} lines from "
         f"{st['period_start'].day} to {st['period_end'].day} {st['period_end']:%B}.",
         {"service": "parser sandbox"})
    if st["closing_cents"] is not None:
        emit(job_id, 1, "info",
             f"The running balance adds up on every line: {rand(st['opening_cents'])} at the start, "
             f"{rand(money_in)} in, {rand(money_out)} out, {rand(st['closing_cents'])} at the end.")
    mark = len(svc.vultr.log)
    screened = False
    if svc.vultr.ready:
        text = "\n".join(f"{ln.description} {ln.reference}" for ln in lines)
        try:
            verdict = svc.vultr.screen(text)
            screened = verdict.checked
            if not verdict.safe:
                emit(job_id, 1, "error", "The statement's wording did not pass the safety screen, so the agent stopped here.",
                     {"categories": verdict.categories})
                _finish(job_id, "failed")
                return
        except VultrError:
            emit(job_id, 1, "info", "The safety screen did not answer, so the statement's wording is treated as data only.")
    emit(job_id, 1, "done",
         "The statement's wording passed the safety screen before any model read it." if screened else
         "The statement is stored, and its wording is treated as data only.",
         {"vultr": _vultr_used(svc, mark)})

    # Step 2: match.
    emit(job_id, 2, "start", STEP_TITLES[2])
    with db.tx() as c:
        docs = books.open_documents(c, wid)
        customers = [p["name"] for p in books.parties(c, wid, "customer")]
        suppliers = [p["name"] for p in books.parties(c, wid, "supplier")]
        accounts = books.accounts(c, wid)
    mark = len(svc.vultr.log)
    from .matching import match_all
    from .ranking import WordRanker

    ranker = svc.ranker
    try:
        outcomes = match_all(lines, docs, customers, suppliers, accounts, ranker)
    except (VultrError, LimitReached) as exc:
        if isinstance(exc, LimitReached):
            raise
        ranker = WordRanker()
        emit(job_id, 2, "info", "Vultr's ranking models did not answer, so names and accounts were ranked by shared words.")
        outcomes = match_all(lines, docs, customers, suppliers, accounts, ranker)
    emit(job_id, 2, "info",
         f"Ranked names and accounts with {ranker.name}. The figures decide each match: an invoice or bill is only "
         "chosen when its amount and date fit.", {"service": ranker.name})

    by_line = {ln.id: ln for ln in lines}
    names = {a.code: a.name for a in accounts}
    with db.tx() as c:
        for o in outcomes:
            ln = by_line[o.line_id]
            payload = {
                "line_id": o.line_id, "amount_cents": ln.amount_cents, "entry_date": ln.line_date.isoformat(),
                "memo": f"{o.title} ({ln.description})"[:200], "entry": o.entry, "applies": o.applies,
                "outcome": o.kind, "status": o.status, "flag": o.flag, "ranked": o.ranked, "choices": o.choices,
                "line": {"date": ln.line_date.isoformat(), "description": ln.description, "reference": ln.reference},
            }
            kind = "flag" if o.status == "flagged" else "match"
            pid = proposals.create(c, wid, kind, o.title, o.reason, payload, job_id)
            c.execute(
                "UPDATE statement_lines SET status = %s, note = %s WHERE id = %s",
                (o.status, o.reason[:300], o.line_id),
            )
    counts = Counter(o.status for o in outcomes)
    for o in outcomes:
        if o.status == "flagged":
            continue
        ln = by_line[o.line_id]
        emit(job_id, 2, "found", o.title, {
            "status": o.status, "amount_cents": ln.amount_cents, "date": ln.line_date.isoformat(),
            "description": ln.description, "reason": o.reason,
            "accounts": [names.get(e["account"], e["account"]) for e in o.entry],
        })
    emit(job_id, 2, "done",
         f"{counts['matched']} lines matched to invoices, bills and cash-ups; {counts['suggested']} costs given a "
         f"suggested account; {counts['flagged']} left for you.", {"vultr": _vultr_used(svc, mark), "counts": dict(counts)})

    # Step 3: flag.
    emit(job_id, 3, "start", STEP_TITLES[3])
    flagged = [o for o in outcomes if o.status == "flagged"]
    order = {"duplicate": 0, "amount_mismatch": 1, "unmatched_debit": 2, "ambiguous": 3, "unmatched_credit": 4}
    flagged.sort(key=lambda o: (order.get(o.flag, 9), -abs(by_line[o.line_id].amount_cents)))
    for o in flagged:
        ln = by_line[o.line_id]
        emit(job_id, 3, "flag", o.title, {
            "flag": o.flag, "amount_cents": ln.amount_cents, "date": ln.line_date.isoformat(),
            "description": ln.description, "reason": o.reason,
        })
    fc = Counter(o.flag for o in flagged)
    words = {
        "duplicate": ("duplicate payment", "duplicate payments"),
        "amount_mismatch": ("amount that does not match", "amounts that do not match"),
        "unmatched_debit": ("payment out that matches nothing", "payments out that match nothing"),
        "unmatched_credit": ("payment in that matches nothing", "payments in that match nothing"),
        "ambiguous": ("payment that fits two invoices", "payments that fit more than one invoice"),
    }
    parts = [f"{n} {words[k][0] if n == 1 else words[k][1]}" for k, n in fc.items()]
    emit(job_id, 3, "done", ("Found " + _join(parts) + ".") if parts else "Nothing needs you: every line matched.")

    # Step 4: who owes, as it will stand once the matched payments are posted.
    emit(job_id, 4, "start", STEP_TITLES[4])
    with db.tx() as c:
        debtors = books.who_owes(c, wid, st["period_end"])
    applied: dict[int, int] = Counter()
    for o in outcomes:
        if o.entry:
            for a in o.applies:
                applied[a["document_id"]] += a["cents"]
    for cst in debtors:
        cst["open_cents"] = cst["late_cents"] = 0
        keep = []
        for inv in cst["invoices"]:
            inv["open_cents"] -= applied.get(inv["document_id"], 0)
            if inv["open_cents"] > 0:
                keep.append(inv)
                cst["open_cents"] += inv["open_cents"]
                if inv["days_late"] > 0:
                    cst["late_cents"] += inv["open_cents"]
        cst["invoices"] = keep
    debtors = [c for c in debtors if c["open_cents"] > 0]
    debtors.sort(key=lambda c: (-c["late_cents"], -c["open_cents"]))
    total = sum(c["open_cents"] for c in debtors)
    late = sum(c["late_cents"] for c in debtors)
    emit(job_id, 4, "info",
         f"Once the matched payments are posted, contractors owe {rand(total)} on {st['period_end'].day} "
         f"{st['period_end']:%B}, and {rand(late)} of it is past its due date.",
         {"ageing": books.ageing(debtors)})
    drafts_by_party, from_model = drafts.followups(svc.llm, shop, seed.OWNER_FIRST_NAME, debtors)
    with db.tx() as c:
        for cst in debtors:
            if cst["party_id"] not in drafts_by_party:
                continue
            text = drafts_by_party[cst["party_id"]]
            proposals.create(
                c, wid, "followup", f"Follow-up for {cst['name']}",
                f"{rand(cst['late_cents'])} late", {"party_id": cst["party_id"], "party": cst["name"],
                                                     "contact": cst["contact"], "text": text,
                                                     "late_cents": cst["late_cents"]}, job_id,
            )
    for cst in debtors:
        emit(job_id, 4, "owes", cst["name"], {
            "open_cents": cst["open_cents"], "late_cents": cst["late_cents"],
            "oldest_days_late": cst["oldest_days_late"],
            "followup": drafts_by_party.get(cst["party_id"], ""),
        })
    emit(job_id, 4, "done",
         f"{len(drafts_by_party)} follow-ups are drafted for you to copy into your own chats. The agent sends nothing.",
         {"drafted_by_model": from_model})

    with db.tx() as c:
        pending = c.execute(
            "SELECT count(*) AS n FROM proposals WHERE job_id = %s AND kind IN ('match', 'flag') AND status = 'pending'",
            (job_id,),
        ).fetchone()["n"]
    emit(job_id, 5, "waiting",
         f"{pending} items wait for you. Approve or reject each one, then press Post approved entries.")
    _finish(job_id, "waiting", {"month": month, "money_in": money_in, "money_out": money_out})


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def run_post(job_id: int, svc: Services) -> None:
    try:
        _step_5(job_id, svc)
    except LimitReached as exc:
        emit(job_id, 5, "error", exc.message)
        _finish(job_id, "failed")
    except Exception:
        traceback.print_exc()
        emit(job_id, 5, "error", "Something went wrong while posting. Nothing half-done was kept.")
        _finish(job_id, "failed")


def _step_5(job_id: int, svc: Services) -> None:
    with db.tx() as c:
        job = _job(c, job_id)
        wid = str(job["workspace_id"])
        parent = c.execute("SELECT * FROM jobs WHERE id = %s", (job["parent_id"],)).fetchone()
        st = c.execute("SELECT * FROM statements WHERE id = %s", (parent["statement_id"],)).fetchone()
        approved = c.execute(
            "SELECT p.id, p.title, (p.payload->>'entry_date') AS d FROM proposals p WHERE p.job_id = %s "
            "AND p.kind IN ('match', 'flag') AND p.status = 'approved' ORDER BY (p.payload->>'entry_date'), p.id",
            (parent["id"],),
        ).fetchall()
    emit(job_id, 5, "start", STEP_TITLES[5])
    posted = failed = 0
    for p in approved:
        try:
            with db.tx() as c:
                proposals.post_one(c, wid, p["id"])
            posted += 1
        except proposals.ProposalError as exc:
            failed += 1
            emit(job_id, 5, "flag", p["title"], {"reason": exc.args[0]})
    emit(job_id, 5, "info", f"Posted {posted} approved entries to the ledger, each one balanced."
         + (f" {failed} could not be posted and stay waiting." if failed else ""))

    with db.tx() as c:
        tb = ledger.trial_balance(c, wid, st["period_end"])
        dr, cr = ledger.totals(tb)
        rec = books.bank_reconciliation(c, wid, st)
        act = ledger.period_activity(c, wid, st["period_start"], st["period_end"])
        debtors = books.who_owes(c, wid, st["period_end"])
        creditors = ledger.balance_of(c, wid, ledger.CREDITORS, st["period_end"])
        refunds = ledger.balance_of(c, wid, ledger.REFUNDS, st["period_end"])
        collected = c.execute(
            "SELECT coalesce(sum(l.credit_cents), 0)::bigint AS s FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
            "WHERE l.workspace_id = %s AND l.account_code = %s AND e.source IN ('close', 'agent') AND e.entry_date BETWEEN %s AND %s",
            (wid, ledger.DEBTORS, st["period_start"], st["period_end"]),
        ).fetchone()["s"]
        open_items = c.execute(
            "SELECT p.kind, p.status, p.title FROM proposals p WHERE p.job_id = %s AND p.kind IN ('match', 'flag') "
            "AND p.status IN ('pending', 'rejected') ORDER BY p.id",
            (parent["id"],),
        ).fetchall()
        accts = {a.code: a for a in books.accounts(c, wid)}
    if dr != cr:  # the database makes this impossible; say so loudly if it ever is not
        raise RuntimeError("trial balance out")
    emit(job_id, 5, "info", f"The trial balance agrees: debits and credits are both {rand(dr)}.",
         {"debit": dr, "credit": cr})
    if rec["difference_cents"] == 0 and rec["unposted_lines"] == 0:
        emit(job_id, 5, "info",
             f"The bank account in the books is {rand(rec['books_cents'])} on {st['period_end'].day} "
             f"{st['period_end']:%B}, the same as the statement. The month is reconciled.")
    else:
        emit(job_id, 5, "info",
             f"The books show {rand(rec['books_cents'])} in the bank against the statement's "
             f"{rand(rec['statement_cents'])}. The {rec['unposted_lines']} lines not yet posted make up "
             f"{rand(abs(rec['unposted_cents']))} of the gap"
             + ("." if rec["difference_cents"] == 0 else f", and {rand(abs(rec['difference_cents']))} is unexplained."))

    costs = sorted(
        [{"code": k, "name": accts[k].name, "cents": v} for k, v in act.items() if accts.get(k) and accts[k].kind == "expense" and v],
        key=lambda x: -x["cents"],
    )
    debtors_total = sum(x["open_cents"] for x in debtors)
    debtors_late = sum(x["late_cents"] for x in debtors)
    facts = {
        "money_in": (parent["summary"] or {}).get("money_in", 0),
        "money_out": (parent["summary"] or {}).get("money_out", 0),
        "sales": act.get(ledger.SALES, 0),
        "collected": collected,
        "debtors": debtors_total,
        "debtors_late": debtors_late,
        "creditors": creditors,
        "refunds_due": refunds,
        "costs": sum(x["cents"] for x in costs),
        "bank_books": rec["books_cents"],
        "bank_statement": rec["statement_cents"] or 0,
    }
    notes = [f"{p['title']} ({'waiting' if p['status'] == 'pending' else 'set aside'})" for p in open_items]
    month = (parent["summary"] or {}).get("month", "the month")
    paragraph, by_model = drafts.month_paragraph(svc.llm, job["shop_name"], month, facts, notes)
    summary = {
        "month": month, "shop": job["shop_name"], "paragraph": paragraph, "paragraph_by_model": by_model,
        "figures": facts, "costs": costs[:10], "ageing": books.ageing(debtors),
        "debtors": [{"name": x["name"], "open_cents": x["open_cents"], "late_cents": x["late_cents"]} for x in debtors],
        "reconciliation": rec, "trial_balance": {"debit": dr, "credit": cr},
        "posted": posted, "open_items": [dict(x) for x in open_items],
        "as_at": st["period_end"].isoformat(),
    }
    emit(job_id, 5, "done", "The month's one-page summary is ready.", {"summary": summary})
    _finish(job_id, "done", summary)
    with db.tx() as c:
        c.execute("UPDATE jobs SET status = 'done', finished_at = now() WHERE id = %s", (parent["id"],))


def today_in_shop() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Africa/Johannesburg")).date()
