"""The books every visitor's copy starts from: a building-supply merchant in South Africa whose
contractors buy on 30-day accounts, at the start of November 2026, with October still to close.

Nothing here is a real business. Every name was checked against business listings before it was
used, and the data holds no phone number, email address, bank account or company number.

The October bank statement is built from the same facts, so its running balance always adds up,
and it carries the things a real month throws up: a payment that covers two invoices, a short
payment, a supplier paid twice, a supplier overpaid, a deposit short of the week's takings, two
open invoices of the same amount, and a transfer nobody explained.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

import psycopg

from . import ledger
from .ledger import Line

SHOP = "Lesedi Ridge Build Supply"
OWNER_FIRST_NAME = "Lerato"
PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 31)
OPENING_DATE = date(2026, 9, 30)

# Contractors on 30-day accounts, and the first name of the person the owner deals with there.
CUSTOMERS = {
    "fourie": ("Fourie Steel Fixers", "Jaco"),
    "mosia": ("Mosia Plaster & Paint", "Palesa"),
    "msimang": ("Msimang Drywall & Ceilings", "Ayanda"),
    "radebe": ("Radebe Glazing & Aluminium", "Thandeka"),
    "ntuli": ("Ntuli Shopfitters", "Sibusiso"),
    "pillay": ("Pillay Fencing & Gates", "Ravi"),
    "naidoo": ("Naidoo Tile Studio", "Priya"),
}

SUPPLIERS = {
    "nalane": "Nalane Cement Traders",
    "thabeng": "Thabeng Roof Sheeting",
    "marumo": "Marumo Steel Supply",
}


@dataclass(frozen=True)
class Doc:
    number: str
    party: str  # key into CUSTOMERS or SUPPLIERS
    issued: date
    due: date
    cents: int
    memo: str


# Invoices still open on 30 September (they make up the opening debtors balance).
OPEN_INVOICES = (
    Doc("INV-1024", "pillay", date(2026, 7, 28), date(2026, 8, 27), 985_000, "Fencing poles and razor wire"),
    Doc("INV-1031", "pillay", date(2026, 8, 14), date(2026, 9, 13), 2_760_000, "Cement, sand and stone for gate posts"),
    Doc("INV-1036", "fourie", date(2026, 9, 3), date(2026, 10, 3), 1_894_000, "Rebar and binding wire"),
    Doc("INV-1038", "mosia", date(2026, 9, 7), date(2026, 10, 7), 1_241_550, "Plaster sand, cement and primer"),
    Doc("INV-1040", "msimang", date(2026, 9, 10), date(2026, 10, 10), 3_128_000, "Gypsum board and metal studs"),
    Doc("INV-1041", "radebe", date(2026, 9, 12), date(2026, 10, 12), 736_500, "Silicone, sealant and fixings"),
    Doc("INV-1043", "ntuli", date(2026, 9, 18), date(2026, 10, 18), 1_562_000, "Board, edging and hinges"),
    Doc("INV-1045", "naidoo", date(2026, 9, 24), date(2026, 10, 24), 899_000, "Tile adhesive and grout"),
    Doc("INV-1046", "pillay", date(2026, 9, 26), date(2026, 10, 26), 899_000, "Welded mesh and binding wire"),
)

# Invoices raised in October, already in the books before the close.
OCTOBER_INVOICES = (
    Doc("INV-1047", "radebe", date(2026, 10, 2), date(2026, 11, 1), 421_000, "Aluminium angle and screws"),
    Doc("INV-1048", "fourie", date(2026, 10, 5), date(2026, 11, 4), 2_248_000, "Rebar, mesh and spacers"),
    Doc("INV-1049", "msimang", date(2026, 10, 8), date(2026, 11, 7), 1_690_000, "Cornices, board and screws"),
    Doc("INV-1050", "mosia", date(2026, 10, 13), date(2026, 11, 12), 564_000, "Exterior paint and rollers"),
    Doc("INV-1051", "ntuli", date(2026, 10, 16), date(2026, 11, 15), 927_500, "Laminated board and drawer runners"),
    Doc("INV-1052", "pillay", date(2026, 10, 20), date(2026, 11, 19), 1_345_000, "Steel tubing for gates"),
    Doc("INV-1053", "naidoo", date(2026, 10, 22), date(2026, 11, 21), 672_000, "Tile adhesive, trims and spacers"),
    Doc("INV-1054", "fourie", date(2026, 10, 27), date(2026, 11, 26), 318_000, "Tying wire and bar chairs"),
)

OPEN_BILLS = (
    Doc("NCT-5521", "nalane", date(2026, 9, 15), date(2026, 10, 15), 4_830_000, "Cement, 50 kg bags"),
    Doc("TRS-2290", "thabeng", date(2026, 9, 20), date(2026, 10, 20), 2_176_000, "Roof sheeting and ridging"),
    Doc("MSS-0874", "marumo", date(2026, 9, 25), date(2026, 10, 25), 3_645_000, "Rebar and mesh"),
)

OCTOBER_BILLS = (
    Doc("NCT-5560", "nalane", date(2026, 10, 12), date(2026, 11, 11), 5_210_000, "Cement, 50 kg bags"),
    Doc("TRS-2311", "thabeng", date(2026, 10, 18), date(2026, 11, 17), 1_488_000, "IBR sheeting"),
)

# The counter's cash takings, counted each Sunday and banked on the Monday.
CASHUPS = (
    ("CU-2026-10-04", date(2026, 10, 4), 1_425_000),
    ("CU-2026-10-11", date(2026, 10, 11), 1_678_000),
    ("CU-2026-10-18", date(2026, 10, 18), 1_390_500),
    ("CU-2026-10-25", date(2026, 10, 25), 1_564_000),
)

OPENING_BANK = 18_425_040
OPENING_CASH = 680_000
OPENING_STOCK = 41_200_000

# The October statement, in the bank's order. Positive is money in.
STATEMENT_ROWS: tuple[tuple[date, str, str, int], ...] = (
    (date(2026, 10, 1), "DEBIT ORDER YARD RENT OCTOBER", "RENT10", -1_850_000),
    (date(2026, 10, 2), "EFT CREDIT FOURIE STEEL FIX", "INV1036", 1_894_000),
    (date(2026, 10, 3), "PREPAID ELECTRICITY METER 0412", "", -245_000),
    (date(2026, 10, 5), "CASH DEPOSIT BRANCH", "", 1_425_000),
    (date(2026, 10, 5), "CASH DEPOSIT FEE", "", -3_562),
    (date(2026, 10, 6), "FUEL DELIVERY TRUCK", "CARD 4471", -186_040),
    (date(2026, 10, 9), "EFT CREDIT MOSIA PLASTER", "INV-1038", 1_200_000),
    (date(2026, 10, 9), "CASH WITHDRAWAL BRANCH", "", -200_000),
    (date(2026, 10, 12), "CASH DEPOSIT BRANCH", "", 1_678_000),
    (date(2026, 10, 12), "CASH DEPOSIT FEE", "", -4_195),
    (date(2026, 10, 12), "EFT CREDIT MSIMANG DRYWALL", "OCT PAYMENT", 3_128_000),
    (date(2026, 10, 13), "FUEL DELIVERY TRUCK", "CARD 4471", -210_575),
    (date(2026, 10, 14), "EFT CREDIT RADEBE GLAZING", "INV1041 INV1047", 1_157_500),
    (date(2026, 10, 14), "PAYMENT NALANE CEMENT", "NCT5521", -4_830_000),
    (date(2026, 10, 15), "AIRTIME AND DATA SHOP PHONES", "", -64_900),
    (date(2026, 10, 16), "WORKSHOP REPAIRS FORKLIFT", "JOB 3381", -438_000),
    (date(2026, 10, 17), "INTERNET TRANSFER", "", -350_000),
    (date(2026, 10, 19), "CASH DEPOSIT BRANCH", "", 1_390_500),
    (date(2026, 10, 19), "CASH DEPOSIT FEE", "", -3_476),
    (date(2026, 10, 19), "PAYMENT THABENG ROOF", "TRS2290", -2_176_000),
    (date(2026, 10, 20), "FUEL DELIVERY TRUCK", "CARD 4471", -194_410),
    (date(2026, 10, 21), "PAYMENT THABENG ROOF", "TRS2290", -2_176_000),
    (date(2026, 10, 21), "PREPAID ELECTRICITY METER 0412", "", -120_000),
    (date(2026, 10, 23), "EFT CREDIT NAIDOO TILES", "", 899_000),
    (date(2026, 10, 24), "PAYMENT MARUMO STEEL", "MSS0874", -3_694_500),
    (date(2026, 10, 25), "SALARIES OCTOBER YARD AND COUNTER", "", -3_860_000),
    (date(2026, 10, 26), "CASH DEPOSIT BRANCH", "", 1_544_000),
    (date(2026, 10, 26), "CASH DEPOSIT FEE", "", -3_860),
    (date(2026, 10, 27), "WATER AND RATES OCTOBER", "", -173_520),
    (date(2026, 10, 28), "ALARM MONITORING OCTOBER", "", -89_900),
    (date(2026, 10, 29), "EFT CREDIT PILLAY FENCING", "", 500_000),
    (date(2026, 10, 30), "EFT CREDIT FOURIE SF", "INV 1054", 318_000),
    (date(2026, 10, 31), "MONTHLY ACCOUNT FEE", "", -18_900),
)


def statement_csv() -> bytes:
    """The October statement as the bank exports it, with a running balance."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Description", "Reference", "Amount", "Balance"])
    bal = OPENING_BANK
    for d, desc, ref, cents in STATEMENT_ROWS:
        bal += cents
        w.writerow([d.isoformat(), desc, ref, _fmt(cents), _fmt(bal)])
    return buf.getvalue().encode()


def _fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def statement_closing() -> int:
    return OPENING_BANK + sum(c for *_x, c in STATEMENT_ROWS)


def seed(conn: psycopg.Connection, workspace_id: str) -> None:
    """Write the books into a new, empty copy inside the caller's transaction."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO accounts (workspace_id, code, name, kind, hint) VALUES (%s, %s, %s, %s, %s)",
            [(workspace_id, code, name, kind, hint) for code, name, kind, hint in ledger.CHART],
        )

    party_ids: dict[str, int] = {}
    for key, (name, contact) in CUSTOMERS.items():
        r = conn.execute(
            "INSERT INTO parties (workspace_id, kind, name, contact, terms_days) VALUES (%s, 'customer', %s, %s, 30) RETURNING id",
            (workspace_id, name, contact),
        ).fetchone()
        party_ids[key] = r["id"]
    for key, name in SUPPLIERS.items():
        r = conn.execute(
            "INSERT INTO parties (workspace_id, kind, name, terms_days) VALUES (%s, 'supplier', %s, 30) RETURNING id",
            (workspace_id, name),
        ).fetchone()
        party_ids[key] = r["id"]

    def add_doc(kind: str, d: Doc) -> int:
        r = conn.execute(
            "INSERT INTO documents (workspace_id, kind, number, party_id, issued_on, due_on, total_cents, memo) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (workspace_id, kind, d.number, party_ids[d.party], d.issued, d.due, d.cents, d.memo),
        ).fetchone()
        return r["id"]

    # Opening balances on 30 September, with each open invoice and bill on its own line so the
    # debtors and creditors control accounts always equal the documents behind them.
    lines: list[Line] = [
        Line(ledger.BANK, debit=OPENING_BANK),
        Line(ledger.CASH, debit=OPENING_CASH),
        Line("1200", debit=OPENING_STOCK),
    ]
    for d in OPEN_INVOICES:
        lines.append(Line(ledger.DEBTORS, debit=d.cents, document_id=add_doc("invoice", d)))
    for d in OPEN_BILLS:
        lines.append(Line(ledger.CREDITORS, credit=d.cents, document_id=add_doc("bill", d)))
    capital = sum(ln.debit for ln in lines) - sum(ln.credit for ln in lines)
    lines.append(Line("3000", credit=capital))
    ledger.post(conn, workspace_id, OPENING_DATE, "Opening balances", lines, source="opening")

    for d in OCTOBER_INVOICES:
        doc_id = add_doc("invoice", d)
        name = CUSTOMERS[d.party][0]
        ledger.post(
            conn, workspace_id, d.issued, f"{d.number} to {name}: {d.memo}",
            [Line(ledger.DEBTORS, debit=d.cents, document_id=doc_id), Line(ledger.SALES, credit=d.cents)],
            source="books",
        )
    for d in OCTOBER_BILLS:
        doc_id = add_doc("bill", d)
        ledger.post(
            conn, workspace_id, d.issued, f"{d.number} from {SUPPLIERS[d.party]}: {d.memo}",
            [Line(ledger.PURCHASES, debit=d.cents), Line(ledger.CREDITORS, credit=d.cents, document_id=doc_id)],
            source="books",
        )
    for number, day, cents in CASHUPS:
        r = conn.execute(
            "INSERT INTO documents (workspace_id, kind, number, issued_on, due_on, total_cents, memo) "
            "VALUES (%s, 'cashup', %s, %s, %s, %s, %s) RETURNING id",
            (workspace_id, number, day, day, cents, "Counter cash takings for the week"),
        ).fetchone()
        ledger.post(
            conn, workspace_id, day, f"Counter cash takings, week ending {day.day} {day:%b}",
            [Line(ledger.CASH, debit=cents, document_id=r["id"]), Line(ledger.SALES, credit=cents)],
            source="books",
        )

    conn.execute(
        "INSERT INTO audit_log (workspace_id, actor, action, detail) VALUES (%s, 'system', 'copy_created', %s)",
        (workspace_id, psycopg.types.json.Jsonb({"shop": SHOP})),
    )
