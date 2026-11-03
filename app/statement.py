"""Reading a bank statement CSV. This module runs inside the parser container, which has no
network, so an unsafe file is opened only where it cannot reach anything.

A statement has a header row and one row per transaction. The columns it understands, in any
order and any letter case: Date; Description; Reference (optional); either Amount (money in is
positive, money out negative) or Money in and Money out; and Balance (optional). Errors name a
row and a column, never the cell's content, so nothing a file holds is echoed back.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import asdict, dataclass
from datetime import date, datetime

from .money import AmountError, parse_cents

MAX_ROWS = 400
MAX_FIELD = 140

_ALIASES = {
    "date": "date",
    "transaction date": "date",
    "posting date": "date",
    "description": "description",
    "details": "description",
    "narrative": "description",
    "reference": "reference",
    "ref": "reference",
    "amount": "amount",
    "money in": "money_in",
    "credit": "money_in",
    "credits": "money_in",
    "money out": "money_out",
    "debit": "money_out",
    "debits": "money_out",
    "balance": "balance",
    "running balance": "balance",
}


class StatementError(ValueError):
    """A statement the agent cannot read; the message is safe to show."""


@dataclass(frozen=True)
class StatementLine:
    line_no: int
    line_date: date
    description: str
    reference: str
    amount_cents: int
    balance_cents: int | None


@dataclass(frozen=True)
class Statement:
    sha256: str
    lines: tuple[StatementLine, ...]
    opening_cents: int | None
    closing_cents: int | None
    period_start: date
    period_end: date
    balance_checked: bool

    def as_dict(self) -> dict:
        d = asdict(self)
        d["lines"] = [
            {**asdict(ln), "line_date": ln.line_date.isoformat()} for ln in self.lines
        ]
        d["period_start"] = self.period_start.isoformat()
        d["period_end"] = self.period_end.isoformat()
        return d


def _date(text: str) -> date:
    t = text.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError("date")


def _clean(text: str) -> str:
    # Printable text only, single spaces, a sensible length. Formula prefixes are kept as text
    # but never evaluated anywhere, and the screen shows them as plain characters.
    t = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(t.split())[:MAX_FIELD]


def parse(data: bytes, *, max_bytes: int = 20 * 1024) -> Statement:
    if len(data) > max_bytes:
        raise StatementError(f"The statement is larger than {max_bytes // 1024} KB.")
    if not data.strip():
        raise StatementError("The statement is empty.")
    if b"\x00" in data:
        raise StatementError("The statement is not a text file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:  # pragma: no cover - cp1252 decodes almost anything
            raise StatementError("The statement is not a text file.") from exc

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise StatementError("The statement needs a header row and at least one transaction.")
    if len(rows) - 1 > MAX_ROWS:
        raise StatementError(f"The statement has more than {MAX_ROWS} rows.")

    header = [_ALIASES.get(h.strip().lower()) for h in rows[0]]
    cols = {name: i for i, name in enumerate(header) if name}
    if "date" not in cols or "description" not in cols:
        raise StatementError("The header row needs a Date and a Description column.")
    if "amount" not in cols and not ("money_in" in cols or "money_out" in cols):
        raise StatementError("The header row needs an Amount column, or Money in and Money out.")

    def cell(row: list[str], name: str) -> str:
        i = cols.get(name)
        return row[i] if i is not None and i < len(row) else ""

    lines: list[StatementLine] = []
    for n, row in enumerate(rows[1:], start=1):
        try:
            d = _date(cell(row, "date"))
        except ValueError:
            raise StatementError(f"Row {n}: the date could not be read.") from None
        try:
            if "amount" in cols:
                amount = parse_cents(cell(row, "amount"))
            else:
                money_in = cell(row, "money_in").strip()
                money_out = cell(row, "money_out").strip()
                amount = (parse_cents(money_in) if money_in else 0) - (abs(parse_cents(money_out)) if money_out else 0)
        except AmountError:
            raise StatementError(f"Row {n}: the amount could not be read.") from None
        if amount == 0:
            raise StatementError(f"Row {n}: the amount is zero.")
        balance = None
        if "balance" in cols and cell(row, "balance").strip():
            try:
                balance = parse_cents(cell(row, "balance"))
            except AmountError:
                raise StatementError(f"Row {n}: the balance could not be read.") from None
        desc = _clean(cell(row, "description"))
        if not desc:
            raise StatementError(f"Row {n}: the description is empty.")
        lines.append(
            StatementLine(
                line_no=n,
                line_date=d,
                description=desc,
                reference=_clean(cell(row, "reference")),
                amount_cents=amount,
                balance_cents=balance,
            )
        )

    # Some banks list the newest row first. Put the rows in date order, keeping the bank's own
    # order within a day, so the running balance can be followed.
    if lines[0].line_date > lines[-1].line_date:
        lines.reverse()
    lines.sort(key=lambda ln: ln.line_date)
    dates = [ln.line_date for ln in lines]
    if (max(dates) - min(dates)).days > 62:
        raise StatementError("The statement covers more than two months.")

    # When every row carries a running balance, check it: each balance is the one before plus
    # the row's amount. A statement that fails this was edited or exported wrongly.
    opening = closing = None
    checked = False
    if all(ln.balance_cents is not None for ln in lines):
        first = lines[0]
        opening = first.balance_cents - first.amount_cents
        running = opening
        for ln in lines:
            running += ln.amount_cents
            if running != ln.balance_cents:
                raise StatementError(f"Row {ln.line_no}: the running balance does not add up.")
        closing = running
        checked = True

    return Statement(
        sha256=hashlib.sha256(data).hexdigest(),
        lines=tuple(lines),
        opening_cents=opening,
        closing_cents=closing,
        period_start=min(dates),
        period_end=max(dates),
        balance_checked=checked,
    )
