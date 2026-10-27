"""A UK user: salaries in £ per year, Graduate-visa sponsorship, UK-only questions."""

import pytest

from jobright import answers
from jobright.apply import values
from jobright.apply.fields import FieldSpec, Intent, classify
from jobright.apply.values import JobContext
from jobright.salary import parse_budget


@pytest.fixture
def uk(answer_bank, monkeypatch):
    monkeypatch.setattr(values, "_home_country", lambda: "GB")
    answer_bank.clear()
    answer_bank.update({
        "total_experience_years": 0.5, "current_salary": 28000, "expected_salary": 34000, "salary_unit": "annual",
        "currency": "GBP", "notice_period_days": 0, "work_authorization": "Graduate visa", "needs_sponsorship": True,
        "needs_visa_sponsorship_abroad": True, "degree_grade": "2:1", "driving_licence": False,
        "willing_to_relocate": True, "open_to_onsite": True,
    })
    return answer_bank


def value(label, kind="text", options=None, ctx=None, **kw):
    spec = FieldSpec(n="0", kind=kind, label=label, options=options or [], required=True, **kw)
    r = answers.answer_field(spec, ctx or JobContext(job_id="t", source="reed"))
    return r.value if r else None


def test_salary_in_pounds_follows_the_budget(uk):
    assert value("Expected salary (£ per annum)", "number", ctx=JobContext(job_id="t", budget_max_lpa=50000)) == "34000"
    assert value("Expected salary (£ per annum)", "number", ctx=JobContext(job_id="t", budget_max_lpa=31000)) == "31000"
    ctx = JobContext(job_id="t", budget_max_lpa=25000)
    assert value("What is your expected salary?", ctx=ctx) is None and ctx.flags
    assert value("Current salary (£)", "number") == "28000"
    assert value("Expected salary in thousands (£k)", "number") == "34"


def test_uk_right_to_work_and_sponsorship(uk):
    yn = ["Yes", "No"]
    assert value("Are you legally authorised to work in the UK?", "radio", yn) == "Yes"
    assert value("Will you now or in the future require visa sponsorship?", "radio", yn) == "Yes"
    assert value("Do you require sponsorship to work in the United States?", "radio", yn) == "Yes"
    uk["needs_sponsorship"] = None
    uk["work_authorization"] = "British citizen"
    assert value("Will you require sponsorship?", "radio", yn) == "No"


def test_uk_only_questions(uk):
    assert classify(FieldSpec(n="0", kind="radio", label="Do you hold a full UK driving licence?", options=["Yes", "No"])).intent == Intent.DRIVING
    assert value("Do you hold a full UK driving licence?", "radio", ["Yes", "No"]) == "No"
    assert value("Degree classification", "select", ["First", "2:1", "2:2", "Third"]) == "2:1"
    assert value("How soon can you start?", "text") == "Immediately"


def test_degree_names_beyond_india(uk, monkeypatch):
    from jobright.profile import Education
    p = values._profile()
    monkeypatch.setattr(p, "education", [Education(degree="BSc Computer Science", institution="University of Leeds")])
    yn = ["Yes", "No"]
    assert value("Have you completed a Bachelor's degree?", "radio", yn) == "Yes"
    assert value("Do you hold a Master's degree?", "radio", yn) == "No"


def test_budgets_in_other_currencies_are_ignored():
    assert parse_budget("Salary: £45,000 - £55,000 per annum", "GBP", "annual").max == 55000
    assert parse_budget("₹12L - ₹30L/year", "GBP", "annual") is None
    assert parse_budget("£45,000 - £55,000", "INR", "lpa") is None
