"""The matcher with the scores bge-reranker-v2-m3 really gave the October statement (recorded on the
Vultr VM, 3 Nov 2026, in tests/data/rerank-october.json), and the Vultr client's retry."""

import json
from pathlib import Path

import httpx
import pytest

from app import vultr
from app.matching import match_all
from app.vultr import RERANK_CUTS, Vultr, VultrError

from test_matching import _books

RECORDED = json.loads((Path(__file__).parent / "data" / "rerank-october.json").read_text())


class RecordedRanker:
    """Answers with the recorded reranker scores; shortlists by them too, as embeddings would."""

    name = "recorded bge-reranker-v2-m3"
    cuts = RERANK_CUTS

    def _row(self, query, docs):
        line = RECORDED["lines"][query]
        table = {**line["names"], **line.get("accounts", {})}
        return [table.get(d, 0.0) for d in docs]

    def scores(self, query, docs):
        return self._row(query, docs)

    def shortlist(self, queries, docs, k):
        out = []
        for q in queries:
            s = self._row(q, docs)
            out.append(sorted(range(len(docs)), key=lambda i: s[i], reverse=True)[:k])
        return out


@pytest.fixture(scope="module")
def by_desc():
    lines, docs, customers, suppliers, accounts = _books()
    out = match_all(lines, docs, customers, suppliers, accounts, RecordedRanker())
    found = {}
    for ln, o in zip(sorted(lines, key=lambda x: (x.line_date, x.id)), out):
        found.setdefault(ln.description, []).append(o)
    return found


def test_the_recording_covers_every_line_of_the_statement():
    lines, *_ = _books()
    assert {ln.text for ln in lines} <= set(RECORDED["lines"])
    assert RECORDED["model"] == "bge-reranker-v2-m3"


@pytest.mark.parametrize("desc, code", [
    ("PREPAID ELECTRICITY METER 0412", "6200"),
    ("FUEL DELIVERY TRUCK", "6300"),
    ("WORKSHOP REPAIRS FORKLIFT", "6600"),
    ("WATER AND RATES OCTOBER", "6200"),
    ("ALARM MONITORING OCTOBER", "6700"),
    ("SALARIES OCTOBER YARD AND COUNTER", "6000"),
    ("CASH DEPOSIT FEE", "6400"),
    ("DEBIT ORDER YARD RENT OCTOBER", "6100"),
])
def test_real_reranker_scores_suggest_the_right_account(by_desc, desc, code):
    for o in by_desc[desc]:
        assert o.status == "suggested", (desc, o.ranked)
        assert o.entry[0]["account"] == code


def test_wording_that_names_no_cost_stays_with_the_owner(by_desc):
    for desc in ("INTERNET TRANSFER", "CASH WITHDRAWAL BRANCH"):
        (o,) = by_desc[desc]
        assert o.flag == "unmatched_debit" and o.entry == [] and o.choices


def test_the_duplicate_names_the_supplier(by_desc):
    first, second = by_desc["PAYMENT THABENG ROOF"]
    assert first.status == "matched"
    assert second.flag == "duplicate" and second.title == "Thabeng Roof Sheeting paid twice"


def test_the_figures_still_decide_the_matches(by_desc):
    flags = sorted(o.flag for os in by_desc.values() for o in os if o.status == "flagged")
    matched = sum(o.status == "matched" for os in by_desc.values() for o in os)
    assert matched == 10
    assert flags == ["amount_mismatch"] * 4 + ["duplicate"] + ["unmatched_debit"] * 2


def _vultr(client):
    v = Vultr(client=client)
    v.base, v.key = "https://vultr.test/v1", "test-key"
    return v


def _client(answers):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        status = answers.pop(0)
        body = {"results": [{"index": 0, "relevance_score": 0.9}]} if status == 200 else {"error": "busy"}
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def test_one_busy_answer_is_tried_again(monkeypatch):
    monkeypatch.setattr(vultr, "RETRY_WAIT_S", 0)
    client, seen = _client([502, 200])
    v = _vultr(client)
    assert v.rerank("PAYMENT NALANE CEMENT", ["Nalane Cement Traders"]) == [0.9]
    assert len(seen) == 2


def test_a_lasting_failure_still_fails(monkeypatch):
    monkeypatch.setattr(vultr, "RETRY_WAIT_S", 0)
    client, seen = _client([503, 503, 503])
    with pytest.raises(VultrError):
        _vultr(client).rerank("x", ["y"])
    assert len(seen) == 3


def test_a_refusal_is_not_tried_again(monkeypatch):
    monkeypatch.setattr(vultr, "RETRY_WAIT_S", 0)
    client, seen = _client([401])
    with pytest.raises(VultrError):
        _vultr(client).rerank("x", ["y"])
    assert len(seen) == 1
