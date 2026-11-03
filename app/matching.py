"""Matching bank statement lines to the books.

The rule of this module: code decides every match on amount and date, and a model only ranks.
A ranker orders names (which customer does "EFT CREDIT MOSIA PLASTER" look like?) and accounts
(which expense does "WORKSHOP REPAIRS FORKLIFT" look like?). It never picks an invoice whose
amount and date do not fit, and its order only settles a match when the figures leave a choice.

Every outcome carries the journal entry it would post and a sentence saying why, and nothing is
posted here: the owner approves or rejects each outcome first.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, Sequence

from . import ledger
from .money import rand


class Ranker(Protocol):
    name: str
    cuts: "Cuts"

    def scores(self, query: str, docs: Sequence[str]) -> list[float]:
        """One relevance score from 0 to 1 per document, in the documents' order."""

    def shortlist(self, queries: Sequence[str], docs: Sequence[str], k: int) -> list[list[int]]:
        """For each query, the indexes of the k most similar documents (by embeddings)."""


def _apart(n: int, word: str) -> str:
    """'3 days later', '1 day earlier' or 'on the same day'."""
    return "on the same day" if n == 0 else f"{n} day {word}" if n == 1 else f"{n} days {word}"


@dataclass
class OpenDoc:
    id: int
    kind: str  # invoice, bill or cashup
    number: str
    party: str  # the customer's or supplier's name; empty for a cash-up
    issued: date
    due: date
    open_cents: int


@dataclass
class Account:
    code: str
    name: str
    kind: str
    hint: str


@dataclass
class StLine:
    id: int
    line_date: date
    description: str
    reference: str
    amount_cents: int

    @property
    def text(self) -> str:
        return f"{self.description} {self.reference}".strip()


@dataclass
class Outcome:
    line_id: int
    status: str  # matched, suggested or flagged
    kind: str
    title: str
    reason: str
    entry: list[dict] = field(default_factory=list)  # [{account, debit, credit, document_id}]
    applies: list[dict] = field(default_factory=list)  # [{document_id, number, cents}]
    flag: str = ""  # duplicate, amount_mismatch, unmatched_debit, unmatched_credit, ambiguous
    ranked: dict = field(default_factory=dict)
    choices: list[str] = field(default_factory=list)  # accounts the owner may pick for a flagged debit


@dataclass(frozen=True)
class Cuts:
    """When a ranker's view settles a name or an account. Each ranker scores on its own scale, so each
    brings its own cuts: below them the ranker's view settles nothing and the line is left for the owner."""

    name_sure: float = 0.5       # the lowest score a name ranked first may have
    name_margin: float = 0.15    # how far ahead of the second name it must be to settle a choice
    account_sure: float = 0.35
    account_margin: float = 0.05
    lead: float = 1.0            # and how many times the second's score the first must be
    strong: float = 0.8          # another name this high, with the invoice's own name under `weak`,
    weak: float = 0.3            # stops a lone invoice of the same amount from being matched

    def first(self, s1: float, s2: float = 0.0) -> bool:
        """The name ranked first is believable."""
        return s1 >= self.name_sure and s1 >= self.lead * s2

    def clear(self, s1: float, s2: float = 0.0) -> bool:
        """And clearly ahead of the second, so it can choose between two invoices."""
        return self.first(s1, s2) and s1 - s2 >= self.name_margin

    def account(self, s1: float, s2: float = 0.0) -> bool:
        return s1 >= self.account_sure and s1 - s2 >= self.account_margin and s1 >= self.lead * s2


# The word ranker's scale: shared words score near 1, a stem or initials 0.7 to 0.85.
WORD_CUTS = Cuts()
DUPLICATE_DAYS = 7
DEPOSIT_DAYS = 4
INVOICE_DAYS = 150
BILL_DAYS = 120

_NORM = re.compile(r"[^A-Z0-9]+")


def norm(text: str) -> str:
    return _NORM.sub("", text.upper())


def numbers_cited(text: str, docs: Sequence[OpenDoc]) -> list[OpenDoc]:
    """Open documents whose number the text quotes ("INV1041 INV1047", "INV 1054", "TT2290")."""
    words = re.findall(r"[A-Za-z]+[\s-]?\d+", text)
    keys = {norm(w) for w in words}
    return [d for d in docs if norm(d.number) in keys]


def _entry(*lines: tuple[str, int, int, int | None]) -> list[dict]:
    return [{"account": a, "debit": d, "credit": c, "document_id": doc} for a, d, c, doc in lines]


def _top(scores: dict[str, float]) -> tuple[str, float, float]:
    """The best name, its score and the margin over the next one."""
    if not scores:
        return "", 0.0, 0.0
    order = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, s1 = order[0]
    s2 = order[1][1] if len(order) > 1 else 0.0
    return best, s1, s1 - s2


def match_all(
    lines: Sequence[StLine],
    docs: Sequence[OpenDoc],
    customers: Sequence[str],
    suppliers: Sequence[str],
    accounts: Sequence[Account],
    ranker: Ranker,
    *,
    workers: int = 6,
) -> list[Outcome]:
    """Match every line, oldest first, and return one outcome per line."""
    lines = sorted(lines, key=lambda ln: (ln.line_date, ln.id))
    open_left = {d.id: d.open_cents for d in docs}
    cuts = getattr(ranker, "cuts", WORD_CUTS)

    # Ask the ranker everything up front, in parallel: names for every line, and for money
    # out, the accounts an embedding search puts closest.
    expense_accounts = [a for a in accounts if a.kind == "expense" or a.code in ("3100", ledger.CASH)]
    acc_docs = [f"{a.name}: {a.hint}" for a in expense_accounts]
    debits = [ln for ln in lines if ln.amount_cents < 0]
    short = ranker.shortlist([ln.text for ln in debits], acc_docs, k=min(5, len(acc_docs))) if debits else []
    short_by_line = {ln.id: idx for ln, idx in zip(debits, short)}

    def name_scores(ln: StLine) -> dict[str, float]:
        names = customers if ln.amount_cents > 0 else suppliers
        if not names:
            return {}
        return dict(zip(names, ranker.scores(ln.text, names)))

    def account_scores(ln: StLine) -> dict[str, float]:
        idx = short_by_line.get(ln.id) or []
        if not idx:
            return {}
        picked = [expense_accounts[i] for i in idx]
        s = ranker.scores(ln.text, [acc_docs[i] for i in idx])
        return {a.code: v for a, v in zip(picked, s)}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        names = dict(zip([ln.id for ln in lines], pool.map(name_scores, lines)))
        accs = dict(zip([ln.id for ln in debits], pool.map(account_scores, debits)))

    out: list[Outcome] = []
    paid_out: list[StLine] = []
    for ln in lines:
        def still_open(kind: str) -> list[OpenDoc]:
            return [
                d for d in docs
                if d.kind == kind and open_left[d.id] > 0 and d.issued <= ln.line_date
            ]

        if ln.amount_cents > 0:
            o = _money_in(ln, still_open, open_left, names[ln.id], ranker.name, cuts)
        else:
            o = _money_out(ln, still_open, open_left, names[ln.id], accs.get(ln.id, {}), accounts, paid_out, ranker.name, cuts)
            paid_out.append(ln)
        for a in o.applies:
            open_left[a["document_id"]] -= a["cents"]
        out.append(o)
    return out


def _money_in(ln, still_open, open_left, scores, ranker_name, cuts: Cuts = WORD_CUTS) -> Outcome:
    amt = ln.amount_cents
    invoices = [d for d in still_open("invoice") if (ln.line_date - d.issued).days <= INVOICE_DAYS]
    best, s1, margin = _top(scores)
    ranked = {"ranker": ranker_name, "best": best, "score": round(s1, 3), "margin": round(margin, 3)}

    # 1. The payment quotes invoice numbers.
    cited = numbers_cited(ln.text, invoices)
    if cited:
        total = sum(open_left[d.id] for d in cited)
        if total == amt:
            applies = [{"document_id": d.id, "number": d.number, "cents": open_left[d.id]} for d in cited]
            nums = " and ".join(d.number for d in cited)
            return Outcome(
                ln.id, "matched", "invoice_pair" if len(cited) > 1 else "invoice_payment",
                f"{cited[0].party} paid {nums}",
                f"The payment quotes {nums}, and {rand(amt)} is exactly what was open on "
                f"{'them' if len(cited) > 1 else 'it'}.",
                _entry((ledger.BANK, amt, 0, None), *[(ledger.DEBTORS, 0, a["cents"], a["document_id"]) for a in applies]),
                applies, ranked=ranked,
            )
        if len(cited) == 1:
            d = cited[0]
            open_amt = open_left[d.id]
            if amt < open_amt:
                return Outcome(
                    ln.id, "flagged", "amount_mismatch", f"{d.party} paid less than {d.number}",
                    f"The payment quotes {d.number}, which has {rand(open_amt)} open, but {rand(amt)} came in: "
                    f"{rand(open_amt - amt)} short. Approving records it as a part payment and leaves "
                    f"{rand(open_amt - amt)} owing.",
                    _entry((ledger.BANK, amt, 0, None), (ledger.DEBTORS, 0, amt, d.id)),
                    [{"document_id": d.id, "number": d.number, "cents": amt}],
                    flag="amount_mismatch", ranked=ranked,
                )
            return Outcome(
                ln.id, "flagged", "amount_mismatch", f"{d.party} paid more than {d.number}",
                f"The payment quotes {d.number}, which has {rand(open_amt)} open, but {rand(amt)} came in: "
                f"{rand(amt - open_amt)} more than owed. Nothing is posted until you say what the rest is for.",
                flag="amount_mismatch", ranked=ranked,
            )

    # 2. A cash deposit of the counter's takings.
    t = norm(ln.text)
    if "CASH" in t and "DEPOSIT" in t:
        ups = [d for d in still_open("cashup") if 0 <= (ln.line_date - d.issued).days <= DEPOSIT_DAYS]
        exact = [d for d in ups if open_left[d.id] == amt]
        if exact:
            d = exact[0]
            return Outcome(
                ln.id, "matched", "cash_deposit", f"Takings for the week to {d.issued.day} {d.issued:%b} banked",
                f"{rand(amt)} is exactly the cash counted on {d.issued.day} {d.issued:%B}, banked "
                f"{_apart((ln.line_date - d.issued).days, 'later')}.",
                _entry((ledger.BANK, amt, 0, None), (ledger.CASH, 0, amt, d.id)),
                [{"document_id": d.id, "number": d.number, "cents": amt}], ranked={},
            )
        if len(ups) == 1:
            d = ups[0]
            diff = open_left[d.id] - amt
            word = "less" if diff > 0 else "more"
            return Outcome(
                ln.id, "flagged", "amount_mismatch", f"Deposit {word} than the takings for {d.issued.day} {d.issued:%b}",
                f"The cash counted on {d.issued.day} {d.issued:%B} was {rand(open_left[d.id])}, and {rand(amt)} "
                f"was banked: {rand(abs(diff))} {word}. Approving books the deposit and leaves the difference in "
                "cash on hand, so count the till before you approve.",
                _entry((ledger.BANK, amt, 0, None), (ledger.CASH, 0, amt, d.id)),
                [{"document_id": d.id, "number": d.number, "cents": min(amt, open_left[d.id])}],
                flag="amount_mismatch", ranked={},
            )

    # 3. One or more invoices of exactly this amount: the figures leave the choice, the ranker
    #    orders the names.
    same = [d for d in invoices if open_left[d.id] == amt]
    if same:
        parties = sorted({d.party for d in same}, key=lambda p: scores.get(p, 0.0), reverse=True)
        p1 = parties[0]
        s_p1 = scores.get(p1, 0.0)
        s_p2 = scores.get(parties[1], 0.0) if len(parties) > 1 else 0.0
        sure = cuts.clear(s_p1, s_p2)
        other_strong = best not in parties and s1 >= cuts.strong and s_p1 < cuts.weak
        if (len(parties) == 1 and not other_strong) or sure:
            d = sorted([x for x in same if x.party == p1], key=lambda x: x.issued)[0]
            why = (
                f"{rand(amt)} is exactly what is open on {d.number}, issued {d.issued.day} {d.issued:%B}."
                if len(parties) == 1 else
                f"{len(same)} open invoices are {rand(amt)}. The amount and date fit both, and the payer's name "
                f"ranks {p1} first ({s_p1:.2f} against {s_p2:.2f}), so it is {d.number}."
            )
            return Outcome(
                ln.id, "matched", "invoice_payment", f"{p1} paid {d.number}", why,
                _entry((ledger.BANK, amt, 0, None), (ledger.DEBTORS, 0, amt, d.id)),
                [{"document_id": d.id, "number": d.number, "cents": amt}], ranked=ranked,
            )
        return Outcome(
            ln.id, "flagged", "ambiguous", f"{rand(amt)} fits more than one invoice",
            f"{len(same)} open invoices are {rand(amt)} ({', '.join(d.number + ' ' + d.party for d in same)}), "
            "and the payer's name does not settle which. Choose the invoice before you approve.",
            flag="ambiguous", ranked=ranked,
        )

    # 4. Two invoices of the customer the name ranks first, together making the amount.
    if best and cuts.first(s1, s1 - margin):
        mine = sorted([d for d in invoices if d.party == best], key=lambda x: x.issued)
        for i in range(len(mine)):
            for j in range(i + 1, len(mine)):
                if open_left[mine[i].id] + open_left[mine[j].id] == amt:
                    pair = (mine[i], mine[j])
                    applies = [{"document_id": d.id, "number": d.number, "cents": open_left[d.id]} for d in pair]
                    return Outcome(
                        ln.id, "matched", "invoice_pair", f"{best} paid {pair[0].number} and {pair[1].number}",
                        f"No single invoice is {rand(amt)}, but {pair[0].number} and {pair[1].number} of {best}, "
                        "the name ranked first, add up to it exactly.",
                        _entry((ledger.BANK, amt, 0, None), *[(ledger.DEBTORS, 0, a["cents"], a["document_id"]) for a in applies]),
                        applies, ranked=ranked,
                    )
        # 5. The name is clear but no invoice fits the amount.
        if mine:
            d = mine[0]
            part = min(amt, open_left[d.id])
            rest = amt - part
            note = f" The other {rand(rest)} has nowhere to go yet." if rest else ""
            return Outcome(
                ln.id, "flagged", "amount_mismatch", f"{rand(amt)} from {best} matches no invoice",
                f"The payer's name ranks {best} first ({s1:.2f}), but no open invoice of theirs is {rand(amt)}. "
                f"Approving applies {rand(part)} to their oldest invoice, {d.number}, issued {d.issued.day} "
                f"{d.issued:%B}.{note}",
                _entry((ledger.BANK, part, 0, None), (ledger.DEBTORS, 0, part, d.id)) if not rest else [],
                [{"document_id": d.id, "number": d.number, "cents": part}] if not rest else [],
                flag="amount_mismatch", ranked=ranked,
            )

    return Outcome(
        ln.id, "flagged", "unmatched_credit", f"Money in that matches nothing: {rand(amt)}",
        "No open invoice, cash-up or customer fits this payment. Say what it is before anything is posted.",
        flag="unmatched_credit", ranked=ranked,
    )


def _money_out(ln, still_open, open_left, scores, acc_scores, accounts, paid_out, ranker_name, cuts: Cuts = WORD_CUTS) -> Outcome:
    amt = -ln.amount_cents
    best, s1, margin = _top(scores)
    ranked = {"ranker": ranker_name, "best": best, "score": round(s1, 3), "margin": round(margin, 3)}

    # 1. The same payment again within a week: same amount, same wording.
    for prev in reversed(paid_out):
        if -prev.amount_cents == amt and norm(prev.text) == norm(ln.text) and 0 <= (ln.line_date - prev.line_date).days <= DUPLICATE_DAYS:
            who = best if cuts.first(s1, s1 - margin) else "the payee"
            return Outcome(
                ln.id, "flagged", "duplicate", f"{who} paid twice",
                f"{rand(amt)} went out on {prev.line_date.day} {prev.line_date:%B} with the same wording, "
                f"{_apart((ln.line_date - prev.line_date).days, 'earlier')}. Approving books this second payment as "
                f"money {who} must pay back, so it does not count as a cost.",
                _entry((ledger.REFUNDS, amt, 0, None), (ledger.BANK, 0, amt, None)),
                flag="duplicate", ranked=ranked,
            )

    bills = [d for d in still_open("bill") if (ln.line_date - d.issued).days <= BILL_DAYS]

    # 2. The payment quotes a bill number.
    cited = numbers_cited(ln.text, bills)
    if cited:
        total = sum(open_left[d.id] for d in cited)
        if total == amt:
            applies = [{"document_id": d.id, "number": d.number, "cents": open_left[d.id]} for d in cited]
            nums = " and ".join(d.number for d in cited)
            return Outcome(
                ln.id, "matched", "bill_payment", f"Paid {cited[0].party} for {nums}",
                f"The payment quotes {nums}, and {rand(amt)} is exactly what was owed.",
                _entry(*[(ledger.CREDITORS, a["cents"], 0, a["document_id"]) for a in applies], (ledger.BANK, 0, amt, None)),
                applies, ranked=ranked,
            )
        if len(cited) == 1:
            d = cited[0]
            owed = open_left[d.id]
            if amt > owed:
                extra = amt - owed
                return Outcome(
                    ln.id, "flagged", "amount_mismatch", f"Paid {d.party} more than {d.number}",
                    f"{d.number} was {rand(owed)}, and {rand(amt)} went out: {rand(extra)} too much. Approving "
                    f"settles {d.number} and books {rand(extra)} as money {d.party} must pay back.",
                    _entry((ledger.CREDITORS, owed, 0, d.id), (ledger.REFUNDS, extra, 0, None), (ledger.BANK, 0, amt, None)),
                    [{"document_id": d.id, "number": d.number, "cents": owed}],
                    flag="amount_mismatch", ranked=ranked,
                )
            return Outcome(
                ln.id, "flagged", "amount_mismatch", f"Paid {d.party} less than {d.number}",
                f"{d.number} was {rand(owed)}, and {rand(amt)} went out: {rand(owed - amt)} still owed. Approving "
                "records a part payment.",
                _entry((ledger.CREDITORS, amt, 0, d.id), (ledger.BANK, 0, amt, None)),
                [{"document_id": d.id, "number": d.number, "cents": amt}],
                flag="amount_mismatch", ranked=ranked,
            )

    # 3. A bill of exactly this amount, with the supplier's name ranked first.
    same = [d for d in bills if open_left[d.id] == amt]
    if same:
        parties = sorted({d.party for d in same}, key=lambda p: scores.get(p, 0.0), reverse=True)
        p1 = parties[0]
        if cuts.first(scores.get(p1, 0.0)) or len(parties) == 1 and best == p1:
            d = sorted([x for x in same if x.party == p1], key=lambda x: x.issued)[0]
            return Outcome(
                ln.id, "matched", "bill_payment", f"Paid {p1} for {d.number}",
                f"{rand(amt)} is exactly what {d.number} was, and the payee's name ranks {p1} first.",
                _entry((ledger.CREDITORS, amt, 0, d.id), (ledger.BANK, 0, amt, None)),
                [{"document_id": d.id, "number": d.number, "cents": amt}], ranked=ranked,
            )

    # 4. A cost: the ranker suggests an account; the owner confirms it.
    by_code = {a.code: a for a in accounts}
    code, a1, amargin = _top(acc_scores)
    choices = [c for c, _ in sorted(acc_scores.items(), key=lambda kv: kv[1], reverse=True)]
    aranked = {"ranker": ranker_name, "best": code, "score": round(a1, 3), "margin": round(amargin, 3)}
    if code and cuts.account(a1, a1 - amargin):
        name = by_code[code].name if code in by_code else code
        return Outcome(
            ln.id, "suggested", "expense", f"{name}: {rand(amt)}",
            f"No bill fits this payment. Its wording ranks the account {name} first ({a1:.2f}, next "
            f"{a1 - amargin:.2f}). Approve it if that is what the money was for, or pick another account.",
            _entry((code, amt, 0, None), (ledger.BANK, 0, amt, None)), ranked=aranked, choices=choices,
        )
    return Outcome(
        ln.id, "flagged", "unmatched_debit", f"Money out that matches nothing: {rand(amt)}",
        "No bill fits this payment and its wording does not point to one account. Pick the account it "
        "belongs to, or set it aside to ask the bank.",
        flag="unmatched_debit", ranked=aranked, choices=choices or [c for c in ledger.EXPENSE_CODES],
    )
