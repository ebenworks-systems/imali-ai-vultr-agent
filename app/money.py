"""Money is a whole number of cents everywhere in this app. Floats never touch an amount.

`parse_cents` reads an amount the way it appears in a bank statement or an owner's message
("1,234.50", "R 1 234.50", "-850", "850.5"), and `rand` writes one back for people.
"""

from __future__ import annotations

import re

# A statement amount: an optional sign, an optional "R", digits with optional thousands
# separators (comma, space or thin space), and at most two decimals after a full stop.
_AMOUNT = re.compile(
    r"^\s*(?P<sign>[-+−])?\s*(?:R\s*)?(?P<whole>\d{1,3}(?:[ ,  ]\d{3})+|\d+)(?:\.(?P<frac>\d{1,2}))?\s*$"
)

# The largest amount the app accepts: R10 million. Anything bigger is a typing mistake here.
MAX_CENTS = 1_000_000_000


class AmountError(ValueError):
    """An amount that could not be read. The message never repeats the input."""


def parse_cents(text: str) -> int:
    """Read an amount into signed cents, or raise AmountError."""
    if not isinstance(text, str):
        raise AmountError("not text")
    m = _AMOUNT.match(text)
    if not m:
        raise AmountError("not an amount")
    whole = re.sub(r"[ ,  ]", "", m.group("whole"))
    frac = (m.group("frac") or "").ljust(2, "0")
    cents = int(whole) * 100 + int(frac or "0")
    if cents > MAX_CENTS:
        raise AmountError("too large")
    if m.group("sign") in ("-", "−"):
        cents = -cents
    return cents


def rand(cents: int, *, sign: bool = False) -> str:
    """R1,234.50 for 123450; -R850.00 for -85000. `sign` adds + to money in."""
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise TypeError("cents must be an int")
    neg = cents < 0
    whole, frac = divmod(abs(cents), 100)
    body = f"R{whole:,}.{frac:02d}"
    if neg:
        return "-" + body
    if sign and cents > 0:
        return "+" + body
    return body


_RAND_TOKEN = re.compile(r"(?<![\w])R\s?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.\d{1,2})?(?![\d])")
_NUMBER_TOKEN = re.compile(r"(?<![\w.])(?:R\s?)?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.\d{1,2})?(?![\d])")


def figures_in(text: str) -> set[int]:
    """Every rand amount written in a piece of text ("R18,940.00", "R 18 940"), in cents.

    The agent writes money with an R in front, so this is how the code checks that a draft
    quotes only figures it was given. Invoice numbers and dates carry no R and are not counted.
    """
    found: set[int] = set()
    for m in _RAND_TOKEN.finditer(text):
        try:
            found.add(abs(parse_cents(m.group(0))))
        except AmountError:
            continue
    return found


def numbers_in(text: str) -> set[int]:
    """Every number an owner wrote, with or without an R, read as cents ("2300" is 230000).

    An owner's message rarely says "R": "sold 20 bags for 2300" still names R2,300.00.
    """
    found: set[int] = set()
    for m in _NUMBER_TOKEN.finditer(text):
        try:
            found.add(abs(parse_cents(m.group(0))))
        except AmountError:
            continue
    return found
