"""The real LinkedIn engine against a local page that reproduces what broke it:
a cookie banner, a settings popup that closes the dialog mid-application, a
phone number pre-filled into an experience box, misspelt labels, a
whole-number validation error, a budget-based expected CTC, a passport
question only the user can answer, and the "application was sent" dialog."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from jobright import answers
from jobright.apply import linkedin
from jobright.apply.values import JobContext

FIXTURE = (Path(__file__).parent / "fixtures" / "easy_apply.html").resolve()
PDF = next(Path("data/base").glob("*.pdf"), None) or Path(__file__)


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(linkedin, "screenshot", lambda *a, **k: "")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        yield pg
        browser.close()


def job(query=""):
    return {"id": "linkedin:mock", "url": FIXTURE.as_uri() + query, "source": "linkedin",
            "company": "MockCo", "title": "Flutter Developer"}


def test_submits_through_popup_prefill_and_validation(page, answer_bank):
    answer_bank.update(has_passport="yes", willing_to_travel="yes")
    ctx = JobContext(job_id="linkedin:mock", source="linkedin", budget_max_lpa=35)
    out = linkedin.apply(page, job("?interrupt=1"), PDF, ctx=ctx)

    assert out.status == "applied", (out.status, out.detail, out.missing, out.notes)
    assert "application was sent" in out.evidence.lower()
    sent = page.evaluate("window.submittedAnswers")
    assert sent["q1"] == "6"          # corrected from the pre-filled phone number
    assert sent["q2"] == "6"          # 6.8 rejected as not a whole number, recovered to 6
    assert sent["q3"] == "35"         # budget max 35 < expected 40 → ask for 35
    assert sent["q4"] == "Yes" and sent["q5"] == "Yes"
    assert sent["follow"] is False    # "Follow MockCo" opted out
    assert page.evaluate("window.cookieChoice") == "Reject all"
    notes = " ".join(out.notes)
    assert "reopened the saved draft" in notes and "Not now" in notes
    sources = {a["field"]: a["source"] for a in out.audit}
    assert any("salary rule" in s for s in sources.values())
    # nothing left for you to answer: the account email / phone pre-fills were kept as they were
    assert answers.pending_questions() == []


def test_personal_question_hands_over_instead_of_guessing(page):
    out = linkedin.apply(page, job(), PDF, ctx=JobContext(job_id="linkedin:mock", source="linkedin"))
    assert out.status == "needs_you"
    assert any("passport" in m.lower() for m in out.missing)
    assert page.evaluate("window.submittedAnswers") is None   # nothing was sent


def test_dry_run_stops_at_submit(page, answer_bank):
    answer_bank.update(has_passport="yes", willing_to_travel="yes")
    out = linkedin.apply(page, job(), PDF, dry_run=True, ctx=JobContext(job_id="linkedin:mock", source="linkedin"))
    assert out.status == "dry_run"
    assert "application submitted" not in page.inner_text("body").lower()


def test_removed_posting_is_unavailable(page, tmp_path):
    gone = tmp_path / "gone.html"
    gone.write_text("<html><body><main><h1>Jobs</h1><p>Job id provided may not be valid or the job posting "
                    "has been removed.</p></main></body></html>")
    out = linkedin.apply(page, {**job(), "url": gone.as_uri()}, PDF)
    assert out.status == "unavailable"
