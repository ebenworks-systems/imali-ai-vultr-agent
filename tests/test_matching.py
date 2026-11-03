"""The matcher on the October statement: code decides on amount and date, the ranker orders."""

from collections import Counter

import pytest

from app import ledger, seed
from app.matching import Account, OpenDoc, StLine, match_all
from app.ranking import WordRanker
from app.statement import parse


def _books():
    docs, i = [], 1
    for d in seed.OPEN_INVOICES + seed.OCTOBER_INVOICES:
        docs.append(OpenDoc(i, "invoice", d.number, seed.CUSTOMERS[d.party][0], d.issued, d.due, d.cents))
        i += 1
    for d in seed.OPEN_BILLS:
        docs.append(OpenDoc(i, "bill", d.number, seed.SUPPLIERS[d.party], d.issued, d.due, d.cents))
        i += 1
    for n, day, c in seed.CASHUPS:
        docs.append(OpenDoc(i, "cashup", n, "", day, day, c))
        i += 1
    st = parse(seed.statement_csv())
    lines = [StLine(ln.line_no, ln.line_date, ln.description, ln.reference, ln.amount_cents) for ln in st.lines]
    accounts = [Account(c, n, k, h) for c, n, k, h in ledger.CHART]
    customers = [v[0] for v in seed.CUSTOMERS.values()]
    return lines, docs, customers, list(seed.SUPPLIERS.values()), accounts


@pytest.fixture(scope="module")
def outcomes():
    lines, docs, customers, suppliers, accounts = _books()
    out = match_all(lines, docs, customers, suppliers, accounts, WordRanker())
    by_desc = {}
    for ln, o in zip(sorted(lines, key=lambda x: (x.line_date, x.id)), out):
        by_desc.setdefault(ln.description, []).append(o)
    return out, by_desc


def test_every_line_gets_one_outcome(outcomes):
    out, _ = outcomes
    assert len(out) == len(seed.STATEMENT_ROWS)


def test_every_proposed_entry_balances(outcomes):
    out, _ = outcomes
    for o in out:
        if o.entry:
            ledger.check_lines([ledger.Line(e["account"], e["debit"], e["credit"], e["document_id"]) for e in o.entry])


def test_the_month_throws_up_what_it_should(outcomes):
    out, _ = outcomes
    flags = Counter(o.flag for o in out if o.flag)
    assert flags == {"amount_mismatch": 4, "duplicate": 1, "unmatched_debit": 1}


def test_a_payment_quoting_two_invoices_settles_both(outcomes):
    _, by = outcomes
    o = by["EFT CREDIT RADEBE GLAZING"][0]
    assert o.kind == "invoice_pair" and {a["number"] for a in o.applies} == {"INV-1041", "INV-1047"}


def test_two_invoices_of_the_same_amount_are_settled_by_the_name_not_by_luck(outcomes):
    _, by = outcomes
    o = by["EFT CREDIT NAIDOO TILES"][0]
    assert o.status == "matched"
    assert [a["number"] for a in o.applies] == ["INV-1045"]  # Naidoo's, not Pillay's INV-1046 of the same amount


def test_a_short_payment_is_flagged_with_the_difference(outcomes):
    _, by = outcomes
    o = by["EFT CREDIT MOSIA PLASTER"][0]
    assert o.flag == "amount_mismatch" and "R415.50 short" in o.reason
    assert o.applies == [{"document_id": o.applies[0]["document_id"], "number": "INV-1038", "cents": 1_200_000}]


def test_the_second_identical_supplier_payment_is_a_duplicate(outcomes):
    _, by = outcomes
    first, second = by["PAYMENT THABENG ROOF"]
    assert first.kind == "bill_payment" and second.flag == "duplicate"
    assert second.entry[0]["account"] == ledger.REFUNDS


def test_an_overpaid_bill_books_the_extra_as_money_owed_back(outcomes):
    _, by = outcomes
    o = by["PAYMENT MARUMO STEEL"][0]
    assert o.flag == "amount_mismatch"
    refunds = [e for e in o.entry if e["account"] == ledger.REFUNDS]
    assert refunds and refunds[0]["debit"] == 49_500


def test_a_deposit_short_of_the_takings_is_flagged(outcomes):
    _, by = outcomes
    flagged = [o for o in by["CASH DEPOSIT BRANCH"] if o.flag]
    assert len(flagged) == 1 and "R200.00 less" in flagged[0].reason


def test_a_transfer_nobody_explained_is_left_for_the_owner(outcomes):
    _, by = outcomes
    o = by["INTERNET TRANSFER"][0]
    assert o.flag == "unmatched_debit" and o.entry == [] and o.choices


class OneSidedRanker(WordRanker):
    """A ranker that always prefers Pillay: the figures must still win."""

    name = "biased"

    def scores(self, query, docs):
        return [0.99 if "Pillay" in d else 0.0 for d in docs]


def test_a_ranker_cannot_force_a_match_the_figures_do_not_allow():
    lines, docs, customers, suppliers, accounts = _books()
    out = match_all(lines, docs, customers, suppliers, accounts, OneSidedRanker())
    for o in out:
        for a in o.applies:
            doc = next(d for d in docs if d.id == a["document_id"])
            if doc.kind == "invoice":
                # Whatever the ranker says, money only goes to an invoice the payment can pay.
                assert a["cents"] <= doc.open_cents
    fourie = [o for ln, o in zip(sorted(lines, key=lambda x: (x.line_date, x.id)), out) if ln.description == "EFT CREDIT FOURIE STEEL FIX"][0]
    assert [a["number"] for a in fourie.applies] == ["INV-1036"]  # the quoted invoice number wins
