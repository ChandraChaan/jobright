"""Job budgets and the expected-salary rule, in any currency.

A budget is read from the job's own text and returned in the user's salary
unit: lakhs per annum for India ("₹12L - ₹30L/year", "3M INR/yr", "25-40
LPA", "₹25,00,000 P.A."), annual amounts elsewhere ("£45,000 - £55,000 a
year", "£45k-£55k", "$120K–$150K", "€60.000"). Monthly figures are
annualised; hourly and daily rates are ignored; a budget in another currency
than the user's is ignored rather than compared.

Expected salary for one application:
- budget max ≥ your expected salary          → your expected salary
- current salary ≤ budget max < expected     → the budget max
- budget max < current salary                → flag for your review
- no budget stated                           → your expected salary
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CURRENCY = [
    ("INR", re.compile(r"₹|\binr\b|\brs\.?(?=\s?\d)|\blpa\b|\blakhs?\b|\blacs?\b|\bcrores?\b|\d\s?l\b|\d\s?l/", re.I)),
    ("GBP", re.compile(r"£|\bgbp\b", re.I)),
    ("EUR", re.compile(r"€|\beur\b", re.I)),
    ("USD", re.compile(r"\$|\busd\b", re.I)),
    ("AED", re.compile(r"\baed\b", re.I)),
    ("SGD", re.compile(r"\bsgd\b|s\$", re.I)),
]
_NUM = r"(\d{1,3}(?:[,.]\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_UNIT = r"(lpa|lakhs?|lacs?|lac|cr|crores?|mn|million|m|k|l)?"
_RANGE = re.compile(
    rf"{_NUM}\s*{_UNIT}\b\s*(?:inr|gbp|usd|eur)?\s*(?:/\s*(?:yr|year|annum|mo|month))?\s*(?:p\.?a\.?)?\s*"
    rf"(?:-|–|—|to)\s*(?:₹|£|\$|€|inr|rs\.?|gbp|usd|eur)?\s*{_NUM}\s*{_UNIT}\b",
    re.I,
)
_SINGLE = re.compile(rf"(?:up ?to|upto|max(?:imum)?|budget(?: of)?|ctc(?: of)?|salary:?|₹|£|\$|€|inr|rs\.?)\s*:?\s*{_NUM}\s*{_UNIT}\b", re.I)
_CONTEXT = re.compile(r"₹|£|\$|€|\b(inr|gbp|usd|eur|rs|lpa|lakhs?|lacs?|ctc|salary|compensation|package|budget|pay|per annum|a year)\b", re.I)


@dataclass
class Budget:
    min: float | None
    max: float          # in the user's salary unit (lakhs for "lpa", currency units for "annual")
    currency: str
    text: str

    @property
    def max_lpa(self) -> float:      # older name, used for India
        return self.max


def _number(s: str) -> float:
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):        # European thousands: 60.000
        return float(s.replace(".", ""))
    return float(s.replace(",", ""))


def _annual(value: float, unit: str | None, monthly: bool) -> float | None:
    """A figure as a full annual amount in its currency."""
    u = (unit or "").lower()
    mult = {"lpa": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5,
            "cr": 1e7, "crore": 1e7, "crores": 1e7, "m": 1e6, "mn": 1e6, "million": 1e6, "k": 1e3}.get(u, 1.0)
    amount = value * mult
    if monthly:
        amount *= 12
    return amount


def _currency_of(line: str) -> str | None:
    for code, rx in _CURRENCY:
        if rx.search(line):
            return code
    return None


def parse_budget(text: str, currency: str = "INR", style: str = "lpa") -> Budget | None:
    """The highest plausible salary range in `text`, in the user's salary unit."""
    if not text:
        return None
    best: Budget | None = None
    for line in re.split(r"[\n|]", text):
        if not _CONTEXT.search(line):
            continue
        if re.search(r"per hour|/\s*h(ou)?r\b|hourly|per day|/\s*day\b|daily rate", line, re.I):
            continue
        cur = _currency_of(line)
        if cur is None and not re.search(r"\b(ctc|salary|compensation|package|budget)\b", line, re.I):
            continue
        cur = cur or currency
        if cur != currency:
            continue                     # a budget in another currency is not comparable
        monthly = bool(re.search(r"per month|/\s*mo(nth)?\b|monthly|p\.?m\.?\b|a month", line, re.I))
        found: list[tuple[float | None, float, str]] = []
        for m in _RANGE.finditer(line):
            lo_n, lo_u, hi_n, hi_u = m.groups()
            unit = hi_u or lo_u
            try:
                lo = _annual(_number(lo_n), lo_u or unit, monthly)
                hi = _annual(_number(hi_n), unit, monthly)
            except ValueError:
                continue
            found.append((lo, hi, m.group(0).strip()))
        if not found:
            for m in _SINGLE.finditer(line):
                try:
                    found.append((None, _annual(_number(m.group(1)), m.group(2), monthly), m.group(0).strip()))
                except ValueError:
                    continue
        for lo, hi, raw in found:
            if hi is None:
                continue
            value = hi / 1e5 if style == "lpa" else hi
            low = (lo / 1e5 if style == "lpa" else lo) if lo else None
            plausible = (1 <= value <= 500) if style == "lpa" else (5000 <= value <= 2_000_000)
            if plausible and (best is None or value > best.max):
                best = Budget(low if low and low <= value else None, round(value, 2), cur, raw)
    return best


@dataclass
class CtcDecision:
    expected_lpa: float | None   # the expected salary to give, in the user's unit; None = needs review
    reason: str

    @property
    def expected(self) -> float | None:
        return self.expected_lpa


def _fmt(x: float, style: str) -> str:
    return f"{x:g} LPA" if style == "lpa" else f"{x:,.0f}"


def expected_ctc(budget_max: float | None, current: float, expected: float,
                 override: float | None = None, style: str = "lpa") -> CtcDecision:
    if override is not None:
        return CtcDecision(override, f"set by you for this job: {_fmt(override, style)}")
    if budget_max is None:
        return CtcDecision(expected, "no budget stated; using your expected salary")
    if budget_max >= expected:
        return CtcDecision(expected, f"budget up to {_fmt(budget_max, style)} covers your expected {_fmt(expected, style)}")
    if budget_max >= current:
        return CtcDecision(budget_max, f"budget max {_fmt(budget_max, style)} is below your expected "
                                       f"{_fmt(expected, style)}; asking for the budget max")
    return CtcDecision(None, f"budget max {_fmt(budget_max, style)} is below your current salary {_fmt(current, style)}")
