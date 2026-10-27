"""What each field is asking for, and the exact value typed into it."""

import pytest

from jobright import answers
from jobright.apply.fields import FieldSpec, Intent, classify
from jobright.apply.values import JobContext, recover


def spec(label, kind="text", options=None, **kw):
    return FieldSpec(n="0", kind=kind, label=label, options=options or [], required=True, **kw)


@pytest.mark.parametrize("label,kind,intent,subject", [
    # the bug: "mobile" here is a skill, not a phone number (labels as typed on the real form)
    ("How many years of expereince do you have in Mobile App Developement?", "text", Intent.SKILL_YEARS, "mobile app development"),
    ("Mobile App Development experience (in years)", "number", Intent.SKILL_YEARS, "mobile app development"),
    ("Mobile phone number", "tel", Intent.PHONE, None),
    ("Mobile", "text", Intent.PHONE, None),
    ("Relevant exp in Flutter Development?", "text", Intent.SKILL_YEARS, "flutter development"),
    ("Total Years of Experience", "text", Intent.TOTAL_EXPERIENCE, None),
    ("Do you have experience in IOS, Android, Flutter?", "radio", Intent.HAS_SKILL, "ios android flutter"),
    ("Do you have valid passport and willingness to travel internationally?", "radio", Intent.PASSPORT, None),
    ("What is your level of proficiency in English?", "select", Intent.ENGLISH, None),
    ("What is your Expected CTC?", "text", Intent.EXPECTED_CTC, None),
    ("Are you currently serving notice period?", "radio", Intent.SERVING_NOTICE, None),
    ("How soon you can join?", "text", Intent.NOTICE_PERIOD, None),
    ("Are you comfortable working in a hybrid setting?", "radio", Intent.HYBRID, None),
    ("Year of graduation", "text", Intent.GRADUATION_YEAR, None),
    ("Start date year", "text", Intent.START_YEAR, None),
])
def test_classify(label, kind, intent, subject):
    opts = ["Yes", "No"] if kind == "radio" else []
    r = classify(spec(label, kind, opts))
    assert r.intent == intent
    assert r.subject == subject


def value(label, kind="text", options=None, ctx=None, **kw):
    r = answers.answer_field(spec(label, kind, options, **kw), ctx or JobContext(job_id="t"))
    return r.value if r else None


def test_experience_never_gets_the_phone_number():
    v = value("How many years of expereince do you have in Mobile App Developement?")
    assert v == "6"


def test_years_are_rounded_down_and_respect_whole_number_fields():
    assert value("Total years of experience", "number") == "6.8"
    assert value("Total years of experience", "text", hint="Enter a whole number") == "6"
    assert value("Total years of experience (in months)", "number") == "82"


def test_multi_skill_yes_no_from_profile():
    assert value("Do you have experience in IOS, Android, Flutter?", "radio", ["Yes", "No"]) == "Yes"


def test_personal_questions_are_never_guessed():
    assert value("Do you have valid passport and willingness to travel internationally?", "radio", ["Yes", "No"]) is None
    assert value("What is your level of proficiency in English?", "select", ["Conversational", "Professional"]) is None
    assert value("Relevant exp in Banking domain?") is None          # FILL_ME in the bank
    pending = [p["question"] for p in answers.pending_questions()]
    assert "Do you have valid passport and willingness to travel internationally?" in pending


def test_filled_personal_answers(answer_bank):
    answer_bank.update(english_proficiency="Full professional", has_passport="no", willing_to_travel="yes")
    levels = ["None", "Conversational", "Professional", "Native or bilingual"]
    assert value("What is your level of proficiency in English?", "select", levels) == "Professional"
    assert value("Do you have valid passport and willingness to travel internationally?", "radio", ["Yes", "No"]) == "No"
    assert value("Are you willing to travel for work?", "radio", ["Yes", "No"]) == "Yes"
    answer_bank["has_passport"] = "yes"
    assert value("Do you have valid passport and willingness to travel internationally?", "radio", ["Yes", "No"]) == "Yes"


def test_saved_answer_wins():
    answers.save_answer("Relevant exp in Banking domain?", "4")
    assert value("Relevant exp in Banking domain?") == "4"


def test_units_for_ctc_and_notice():
    assert value("Current CTC (in LPA)", "number") == "30"
    assert value("Current annual CTC in INR", "number") == "3000000"
    assert value("Current salary per month (INR)", "number") == "250000"
    assert value("Notice period (in days)", "number") == "14"
    assert value("Notice period", "select", ["15 days or less", "1 month", "2 months"]) == "15 days or less"
    assert value("Notice period (in weeks)", "number") == "2"
    assert value("Are you currently serving notice period?", "radio", ["Yes", "No"]) == "No"


@pytest.mark.parametrize("budget,expected", [(60, "40"), (40, "40"), (35, "35"), (30, "30"), (None, "40")])
def test_expected_ctc_follows_the_budget(budget, expected):
    assert value("What is your Expected CTC?", ctx=JobContext(job_id="t", budget_max_lpa=budget)) == expected


def test_budget_below_current_ctc_is_flagged_not_answered():
    ctx = JobContext(job_id="t", budget_max_lpa=25)
    assert value("What is your Expected CTC?", ctx=ctx) is None
    assert any(f.startswith("salary review") for f in ctx.flags)
    assert value("What is your Expected CTC?", ctx=JobContext(job_id="t", budget_max_lpa=25, expected_ctc_override=28)) == "28"


def test_phone_formats_for_the_form():
    ctx = JobContext(job_id="t", siblings=["Phone country code", "Mobile phone number"])
    assert value("Mobile phone number", "tel", ctx=ctx) == "9000000001"
    assert value("Phone", "tel") == "+91 9000000001"


def test_sponsorship_for_india_roles():
    assert value("Will you now or in the future require sponsorship?", "radio", ["Yes", "No"]) == "No"
    assert value("Do you require visa sponsorship to work in the US?", "radio", ["Yes", "No"]) == "Yes"


def test_recover_from_site_errors():
    s = spec("Total years of experience")
    interp = classify(s)
    assert recover(["8.7", "8"], ["8.7"], "Enter a whole number between 0 and 99", s, interp) == "8"
    assert recover(["8"], ["8"], "Enter a whole number between 0 and 99", s, interp) is None
    p = spec("Mobile phone number", "tel")
    assert recover(["+91 9000000001"], ["+91 9000000001"], "Please enter a valid 10 digit phone number", p, classify(p)) == "9000000001"
