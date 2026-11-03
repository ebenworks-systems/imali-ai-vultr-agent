import pytest

from app import seed
from app.statement import StatementError, parse


def test_the_october_statement_reads_and_its_running_balance_adds_up():
    st = parse(seed.statement_csv())
    assert len(st.lines) == len(seed.STATEMENT_ROWS)
    assert st.balance_checked
    assert st.opening_cents == seed.OPENING_BANK
    assert st.closing_cents == seed.statement_closing()
    assert st.period_start.isoformat() == "2026-10-01"
    assert st.period_end.isoformat() == "2026-10-31"
    assert all(isinstance(ln.amount_cents, int) for ln in st.lines)


def test_money_in_and_money_out_columns_work():
    data = b"Date,Description,Money in,Money out,Balance\n01/10/2026,Rent,,100.00,900.00\n02/10/2026,Payment,50.5,,950.50\n"
    st = parse(data)
    assert [ln.amount_cents for ln in st.lines] == [-10000, 5050]
    assert st.opening_cents == 100000


def test_newest_first_statements_are_put_in_date_order():
    data = b"Date,Description,Amount,Balance\n2026-10-03,B,-10.00,90.00\n2026-10-01,A,100.00,100.00\n"
    st = parse(data)
    assert [ln.description for ln in st.lines] == ["A", "B"]
    assert st.closing_cents == 9000


def test_a_broken_running_balance_is_refused():
    data = b"Date,Description,Amount,Balance\n2026-10-01,A,100.00,100.00\n2026-10-02,B,-10.00,95.00\n"
    with pytest.raises(StatementError, match="running balance"):
        parse(data)


@pytest.mark.parametrize(
    "data, words",
    [
        (b"", "empty"),
        (b"Date,Amount\n2026-10-01,5\n", "Description"),
        (b"Date,Description,Amount\n2026-10-01,A,lots\n", "Row 1: the amount"),
        (b"Date,Description,Amount\n31/31/2026,A,5\n", "Row 1: the date"),
        (b"Date,Description,Amount\n2026-10-01,A,0\n", "zero"),
        (b"a\x00b", "not a text file"),
    ],
)
def test_errors_name_the_row_not_the_content(data, words):
    with pytest.raises(StatementError) as e:
        parse(data)
    assert words in str(e.value)
    assert "lots" not in str(e.value)


def test_size_and_row_caps():
    big = b"Date,Description,Amount\n" + b"2026-10-01,A,1.00\n" * 2000
    with pytest.raises(StatementError, match="larger than 20 KB"):
        parse(big)
    many = b"Date,Description,Amount\n" + b"2026-10-01,A,1\n" * 401
    with pytest.raises(StatementError, match="more than 400 rows"):
        parse(many, max_bytes=10**6)


def test_descriptions_are_cleaned_and_capped():
    data = ("Date,Description,Amount\n2026-10-01,\"  =HYPERLINK(1)\t\tx" + "y" * 300 + "\",1\n").encode()
    st = parse(data)
    d = st.lines[0].description
    assert len(d) <= 140 and "\t" not in d
