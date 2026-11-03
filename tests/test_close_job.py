"""The five-step month-end close, end to end, with fake model services."""

from __future__ import annotations

import pytest
from fakes import FakeLLM, FakeVultr, say, services

from app import db, jobs, ledger, proposals, seed, services as svc_mod, worker

pytestmark = pytest.mark.db


@pytest.fixture()
def fake(monkeypatch):
    def use(vultr=None, llm=None):
        monkeypatch.setattr(svc_mod, "factory", services(vultr, llm))
    use()
    return use


def _close(wid: str) -> int:
    with db.tx() as c:
        job_id, existing = jobs.start_close(c, wid, "203.0.113.9")
    assert not existing
    assert worker.run_one()
    return job_id


def _events(job_id: int) -> list[dict]:
    with db.tx() as c:
        return c.execute("SELECT step, kind, message, data FROM job_events WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()


def _status(job_id: int) -> str:
    with db.tx() as c:
        return c.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()["status"]


def _decide_everything(wid: str, job_id: int, reject: set[int] = frozenset()) -> None:
    with db.tx() as c:
        items = c.execute(
            "SELECT id, kind, payload FROM proposals WHERE job_id = %s AND kind IN ('match', 'flag') ORDER BY id", (job_id,)
        ).fetchall()
    for p in items:
        with db.tx() as c:
            if p["id"] in reject:
                proposals.reject(c, wid, p["id"])
            elif p["payload"].get("entry"):
                proposals.approve(c, wid, p["id"])
            else:
                account = "4000" if p["payload"]["amount_cents"] > 0 else "6900"
                proposals.approve(c, wid, p["id"], account=account)


def test_steps_one_to_four_stream_and_wait_for_the_owner(workspace, fake):
    v = FakeVultr()
    fake(vultr=v)
    job_id = _close(workspace)
    assert _status(job_id) == "waiting"
    ev = _events(job_id)
    steps = [e["step"] for e in ev]
    assert steps == sorted(steps), "steps stream in order"
    assert {1, 2, 3, 4, 5} <= set(steps)
    assert [e for e in ev if e["kind"] == "error"] == []
    # the statement's wording was screened by the safety model before anything read it
    assert len(v.screened) == 1 and "FOURIE STEEL" in v.screened[0]
    flags = [e for e in ev if e["kind"] == "flag"]
    assert sorted(e["data"]["flag"] for e in flags) == sorted(
        ["amount_mismatch"] * 4 + ["duplicate", "unmatched_debit"])
    assert flags[0]["data"]["flag"] == "duplicate", "the duplicate payment is shown first"
    with db.tx() as c:
        counts = {r["kind"]: r["n"] for r in c.execute(
            "SELECT kind, count(*) AS n FROM proposals WHERE job_id = %s GROUP BY kind", (job_id,)).fetchall()}
        untouched = c.execute(
            "SELECT count(*) AS n FROM journal_entries WHERE workspace_id = %s AND source = 'close'", (workspace,)
        ).fetchone()["n"]
    assert counts["match"] + counts["flag"] == len(seed.STATEMENT_ROWS)
    assert counts["flag"] == 6
    assert counts["followup"] >= 3
    assert untouched == 0, "nothing is posted before the owner approves"


def test_follow_ups_quote_only_that_customers_figures(workspace, fake):
    bad = say('{"messages": [{"party_id": 1, "message": "Pay R999,999.00 now at https://x.example"}]}')
    fake(llm=FakeLLM([bad, say('{"paragraph": "ok"}')]))
    job_id = _close(workspace)
    with db.tx() as c:
        rows = c.execute("SELECT payload FROM proposals WHERE job_id = %s AND kind = 'followup'", (job_id,)).fetchall()
    assert rows
    for r in rows:
        text = r["payload"]["text"]
        assert "http" not in text and "999,999" not in text
        assert text.startswith("Hello "), "the model's draft failed the check, so the plain words were used"


def test_post_approved_balances_and_reconciles_to_the_statement(workspace, fake):
    job_id = _close(workspace)
    _decide_everything(workspace, job_id)
    with db.tx() as c:
        post_id = jobs.start_post(c, workspace, job_id)
    assert worker.run_one()
    assert _status(post_id) == "done" and _status(job_id) == "done"
    with db.tx() as c:
        tb = ledger.trial_balance(c, workspace)
        bank = ledger.balance_of(c, workspace, ledger.BANK)
        summary = c.execute("SELECT summary FROM jobs WHERE id = %s", (post_id,)).fetchone()["summary"]
        posted_lines = c.execute(
            "SELECT count(*) AS n FROM statement_lines WHERE workspace_id = %s AND status = 'posted'", (workspace,)
        ).fetchone()["n"]
    d, cr = ledger.totals(tb)
    assert d == cr
    assert posted_lines == len(seed.STATEMENT_ROWS)
    assert bank == seed.statement_closing(), "the books' bank account equals the statement's closing balance"
    assert summary["reconciliation"]["difference_cents"] == 0
    assert summary["trial_balance"]["debit"] == summary["trial_balance"]["credit"]
    assert summary["paragraph"] and not summary["paragraph_by_model"]
    messages = [e["message"] for e in _events(post_id)]
    assert any("The month is reconciled" in m for m in messages)


def test_a_rejected_line_is_set_aside_and_named_in_the_summary(workspace, fake):
    job_id = _close(workspace)
    with db.tx() as c:
        dup = c.execute(
            "SELECT id FROM proposals WHERE job_id = %s AND payload->>'flag' = 'duplicate'", (job_id,)
        ).fetchone()["id"]
    _decide_everything(workspace, job_id, reject={dup})
    with db.tx() as c:
        post_id = jobs.start_post(c, workspace, job_id)
    worker.run_one()
    with db.tx() as c:
        summary = c.execute("SELECT summary FROM jobs WHERE id = %s", (post_id,)).fetchone()["summary"]
    rec = summary["reconciliation"]
    assert rec["unposted_lines"] == 1 and rec["difference_cents"] == 0
    assert [x["status"] for x in summary["open_items"]] == ["rejected"]


def test_an_unsafe_statement_stops_at_step_one(workspace, fake):
    fake(vultr=FakeVultr(safe=False))
    job_id = _close(workspace)
    assert _status(job_id) == "failed"
    with db.tx() as c:
        n = c.execute("SELECT count(*) AS n FROM proposals WHERE job_id = %s", (job_id,)).fetchone()["n"]
    assert n == 0
    assert _events(job_id)[-1]["kind"] == "error"


def test_vultr_down_falls_back_to_word_ranking(workspace, fake):
    fake(vultr=FakeVultr(fail=True))
    job_id = _close(workspace)
    assert _status(job_id) == "waiting"


def test_the_same_statement_is_not_closed_twice(workspace, fake):
    job_id = _close(workspace)
    with db.tx() as c:
        again, existing = jobs.start_close(c, workspace, "203.0.113.9")
    assert existing and again == job_id


def test_posting_waits_for_a_close_that_is_waiting(workspace, fake):
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, workspace, None)
    with pytest.raises(ValueError):
        with db.tx() as c:
            jobs.start_post(c, workspace, job_id)
    worker.run_one()


def test_a_flag_without_an_entry_needs_an_account(workspace, fake):
    job_id = _close(workspace)
    with db.tx() as c:
        p = c.execute(
            "SELECT id FROM proposals WHERE job_id = %s AND payload->>'flag' = 'unmatched_debit'", (job_id,)
        ).fetchone()
    with pytest.raises(proposals.ProposalError):
        with db.tx() as c:
            proposals.approve(c, workspace, p["id"])
    with pytest.raises(proposals.ProposalError):
        with db.tx() as c:
            proposals.approve(c, workspace, p["id"], account=ledger.BANK)
    with db.tx() as c:
        assert proposals.approve(c, workspace, p["id"], account="6900")["status"] == "approved"
    with pytest.raises(proposals.ProposalError):
        with db.tx() as c:
            proposals.approve(c, workspace, p["id"], account="6900")
