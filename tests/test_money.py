import pytest

from app.money import AmountError, figures_in, numbers_in, parse_cents, rand


@pytest.mark.parametrize(
    "text, cents",
    [
        ("1234.50", 123450),
        ("-850", -85000),
        ("R 1,234.50", 123450),
        ("R1 234.5", 123450),
        ("0.07", 7),
        ("+42", 4200),
        ("−18.20", -1820),
    ],
)
def test_parse_cents_reads_statement_amounts(text, cents):
    assert parse_cents(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1.234", "12,34", "1e5", "NaN", "10000001", "1,2345.00"])
def test_parse_cents_refuses_anything_else(text):
    with pytest.raises(AmountError):
        parse_cents(text)


def test_parse_cents_never_repeats_the_input_in_its_error():
    with pytest.raises(AmountError) as e:
        parse_cents("<script>alert(1)</script>")
    assert "script" not in str(e.value)


def test_rand_formats_cents():
    assert rand(123450) == "R1,234.50"
    assert rand(-85000) == "-R850.00"
    assert rand(7) == "R0.07"
    assert rand(5000, sign=True) == "+R50.00"
    with pytest.raises(TypeError):
        rand(12.5)  # a float never stands for money


def test_figures_in_counts_only_rand_amounts():
    text = "Hello Palesa, INV-1038 of 7 Sep has R415.50 open, and INV-1050 for R 5,640 is due on 12 Nov."
    assert figures_in(text) == {41550, 564000}


def test_numbers_in_reads_an_owners_message():
    assert numbers_in("sold 20 bags of cement to Fourie for 2300") == {2000, 230000}
    assert 245000 in numbers_in("cash sale R2,450 for paint")
