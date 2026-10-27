import pytest

from jobright.salary import expected_ctc, parse_budget


@pytest.mark.parametrize("text,max_lpa", [
    ("Flutter Developer\nBengaluru\n₹12L - ₹30L/year\n3-5 Yrs", 30),
    ("Salary: 3M INR/yr - 5.5M INR/yr", 55),
    ("CTC: 25-40 LPA", 40),
    ("Compensation ₹25,00,000 - 35,00,000 P.A.", 35),
    ("Salary ₹2,50,000 - ₹3,50,000 per month", 42),
    ("Budget up to 42 LPA", 42),
    ("12-30 Lacs P.A.", 30),
])
def test_parse_budget(text, max_lpa):
    b = parse_budget(text)
    assert b is not None and b.max_lpa == max_lpa


@pytest.mark.parametrize("text", [
    "Salary: Not disclosed", "3-5 Yrs experience", "$150,000 - $200,000 a year",
    "VC-funded startup with 8L+ users in 4 months", "",
])
def test_no_budget(text):
    assert parse_budget(text) is None


def test_expected_ctc_rule():
    assert expected_ctc(50, 30, 40).expected_lpa == 40
    assert expected_ctc(40, 30, 40).expected_lpa == 40
    assert expected_ctc(35, 30, 40).expected_lpa == 35
    assert expected_ctc(30, 30, 40).expected_lpa == 30
    assert expected_ctc(25, 30, 40).expected_lpa is None
    assert expected_ctc(None, 30, 40).expected_lpa == 40
    assert expected_ctc(25, 30, 40, override=28).expected_lpa == 28
