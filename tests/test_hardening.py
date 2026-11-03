"""What keeps the public site up for everyone: a copy removed mid-job, one visitor's share of the
model calls, two closes at once, a model that asks for too many tools, and the accounts the
owner may place a bank line in."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeLLM, FakeVultr, say, services

from app import agent, close_job, config, db, jobs, ledger, limits, proposals, seed, services as svc_mod, worker
from app.matching import match_all
from app.ranking import WordRanker
from app.services import Services
from app.vultr import VultrError

from test_matching import _books

pytestmark = pytest.mark.db


@pytest.fixture()
def fake(db_url, monkeypatch):
    monkeypatch.setattr(svc_mod, "factory", services())


@pytest.fixture()
def setting(db_url, monkeypatch):
    def use(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)
        config.reset_settings()
    yield use
    monkeypatch.undo()
    config.reset_settings()


def _copy(addr_hash: str) -> str:
    wid = str(uuid.uuid4())
    with db.tx() as c:
        c.execute(
            "INSERT INTO workspaces (id, expires_at, addr_hash, shop_name, period_start, period_end) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (wid, datetime.now(timezone.utc) + timedelta(hours=2), addr_hash, seed.SHOP, seed.PERIOD_START, seed.PERIOD_END),
        )
        seed.seed(c, wid)
    return wid


def _drop(wid: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM workspaces WHERE id = %s", (wid,))


def _status(job_id: int) -> str | None:
    with db.tx() as c:
        r = c.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return r and r["status"]


# --- Start over while a job runs ------------------------------------------------------------


def test_an_event_for_a_removed_copy_is_dropped(fake):
    wid = _copy("start-over-1")
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, wid, None)
    _drop(wid)
    close_job.emit(job_id, 2, "info", "This event has nowhere to go.")  # raises nothing


def test_a_close_whose_copy_was_removed_ends_quietly(fake):
    wid = _copy("start-over-2")
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, wid, None)
    _drop(wid)
    close_job.run_close(job_id, svc_mod.factory(wid))  # raises nothing
    close_job.run_post(job_id, svc_mod.factory(wid))


def test_start_over_during_the_close_leaves_the_worker_running(fake, monkeypatch):
    wid = _copy("start-over-3")
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, wid, None)

    def removed_midway(jid, *_a):
        _drop(wid)
        raise RuntimeError("the copy went away")

    monkeypatch.setattr(close_job, "_steps_1_to_4", removed_midway)
    close_job.run_close(job_id, svc_mod.factory(wid))  # its error events are dropped, not raised
    assert worker.run_one() is False  # nothing left queued, and the worker is still here


def test_model_calls_for_a_removed_copy_stop(fake):
    wid = _copy("start-over-4")
    _drop(wid)
    with pytest.raises(limits.LimitReached):
        with db.tx() as c:
            limits.model_call(c, "rerank", wid)


def test_any_failure_in_a_job_fails_that_job_only(workspace, fake, monkeypatch):
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, workspace, None)

    def broken(*_a):
        raise RuntimeError("a bug")

    monkeypatch.setattr(close_job, "run_close", broken)
    assert worker.run_one() is True
    assert _status(job_id) == "failed"


# --- the model budget -------------------------------------------------------------------------


def test_one_address_has_its_own_share_of_the_days_model_calls(setting):
    setting("LIMIT_MODEL_CALLS_PER_ADDR_PER_DAY", "3")
    h = f"addr-{uuid.uuid4().hex[:8]}"
    first, second, other = _copy(h), _copy(h), _copy(f"other-{h}")
    try:
        with db.tx() as c:
            limits.model_call(c, "rerank", first)
            limits.model_call(c, "rerank", first)
            limits.model_call(c, "rerank", second)
        with pytest.raises(limits.LimitReached):
            with db.tx() as c:
                limits.model_call(c, "rerank", second)
        with db.tx() as c:  # another visitor is not affected
            limits.model_call(c, "rerank", other)
    finally:
        for wid in (first, second, other):
            _drop(wid)


def test_a_close_started_from_the_chat_counts_against_the_address(workspace):
    addr = "203.0.113.77"
    llm = FakeLLM([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "start_month_close", "arguments": "{}"}}]},
        say("The close has started."),
    ])
    a = agent.Agent(workspace, Services(FakeVultr(), llm, WordRanker()), addr=addr)
    a.reply("Close my month", [])
    assert a.started_job
    with db.tx() as c:
        r = c.execute(
            "SELECT n FROM rate_counters WHERE bucket = %s", (f"close:day:{limits.addr_hash(addr)}",)
        ).fetchone()
    assert r and r["n"] == 1


# --- the reasoning model ----------------------------------------------------------------------


def test_only_the_tool_calls_that_are_answered_are_kept(workspace):
    five = {"content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": "who_owes", "arguments": "{}"}} for i in range(5)
    ]}
    llm = FakeLLM([five, say("Here is who owes.")])
    a = agent.Agent(workspace, Services(FakeVultr(), llm, WordRanker()))
    a.reply("Who owes me money?", [])
    msgs = llm.calls[1][0]
    asked = [t["id"] for m in msgs if m["role"] == "assistant" for t in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert asked == answered == ["c0", "c1", "c2", "c3"]


def test_the_party_is_ranked_with_no_database_connection_held(workspace):
    held = []

    class Watching(WordRanker):
        def scores(self, query, docs):
            st = db.pool().get_stats()
            held.append(st["pool_size"] - st["pool_available"])
            return super().scores(query, docs)

    llm = FakeLLM([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "draft_record",
            "arguments": '{"kind": "stock_on_account", "amount": "4200", "party": "Thabeng Roofing"}'}}]},
        say("Drafted."),
    ])
    a = agent.Agent(workspace, Services(FakeVultr(), llm, Watching()))
    a.reply("Got roof sheets from Thabeng Roofing for 4200 on account", [])
    assert held and max(held) == 0
    assert len(a.drafted) == 1


# --- the ranking pool -------------------------------------------------------------------------


def test_a_failing_ranker_stops_the_queued_calls():
    """Executor.map cancels the calls still queued once one fails, so a ranker that has stopped
    answering costs one call's wait before the word ranker takes over."""
    calls = []

    class Down(WordRanker):
        def shortlist(self, queries, docs, k):  # embeddings still answer, so the pool is reached
            return WordRanker().shortlist(queries, docs, k)

        def scores(self, query, docs):
            calls.append(query)
            raise VultrError("rerank is not answering")

    lines, docs, customers, suppliers, accounts = _books()
    with pytest.raises(VultrError):
        match_all(lines, docs, customers, suppliers, accounts, Down(), workers=1)
    assert len(calls) <= 2 < len(lines)


# --- two closes at once, and a bank line posted once ------------------------------------------


def test_two_closes_at_once_make_one_job(workspace):
    got = {}

    def second():
        with db.tx() as c:
            got["second"] = jobs.start_close(c, workspace, None)

    with db.tx() as c:
        first, existing = jobs.start_close(c, workspace, None)
        t = threading.Thread(target=second)
        t.start()
        t.join(0.5)
        assert t.is_alive()  # it waits for the first close to be saved
    t.join(10)
    assert not existing and got["second"] == (first, True)
    with db.tx() as c:
        n = c.execute("SELECT count(*) AS n FROM statements WHERE workspace_id = %s", (workspace,)).fetchone()["n"]
    assert n == 1


def test_a_bank_line_already_in_the_books_is_not_posted_again(workspace, fake):
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, workspace, None)
    assert worker.run_one()
    with db.tx() as c:
        p = c.execute(
            "SELECT id, payload FROM proposals WHERE job_id = %s AND kind = 'match' AND status = 'pending' "
            "ORDER BY id LIMIT 1", (job_id,)
        ).fetchone()
        proposals.approve(c, workspace, p["id"])
        c.execute("UPDATE statement_lines SET status = 'posted' WHERE id = %s", (p["payload"]["line_id"],))
    with pytest.raises(proposals.ProposalError, match="already in the books"):
        with db.tx() as c:
            proposals.post_one(c, workspace, p["id"])
    with db.tx() as c:
        assert c.execute("SELECT status FROM proposals WHERE id = %s", (p["id"],)).fetchone()["status"] == "approved"


# --- the accounts a bank line may go to -------------------------------------------------------


@pytest.mark.parametrize("account", [ledger.BANK, ledger.DEBTORS, ledger.CREDITORS])
def test_a_flagged_line_never_goes_to_the_bank_or_a_control_account(workspace, fake, account):
    with db.tx() as c:
        job_id, _ = jobs.start_close(c, workspace, None)
    assert worker.run_one()
    with db.tx() as c:
        p = c.execute(
            "SELECT id FROM proposals WHERE job_id = %s AND payload->>'flag' = 'unmatched_debit'", (job_id,)
        ).fetchone()
    with pytest.raises(proposals.ProposalError):
        with db.tx() as c:
            proposals.approve(c, workspace, p["id"], account=account)
