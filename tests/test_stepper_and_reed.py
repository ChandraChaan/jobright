"""The multi-step engine (Reed, Indeed) on a local page, and Reed discovery with a mocked API."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from jobright.apply import stepper, values
from jobright.apply.values import JobContext
from jobright.sources import reed

FIXTURE = (Path(__file__).parent / "fixtures" / "stepper_site.html").resolve()


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(stepper, "screenshot", lambda *a, **k: "")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser.new_page()
        browser.close()


def test_stepper_applies_through_popup(page, answer_bank, monkeypatch):
    monkeypatch.setattr(values, "_home_country", lambda: "GB")
    answer_bank.update(current_salary=28000, expected_salary=34000, salary_unit="annual",
                       work_authorization="British citizen")
    job = {"id": "reed:mock", "url": FIXTURE.as_uri(), "source": "reed", "company": "Example Ltd", "title": "Junior Developer"}
    out = stepper.run(page, job, FIXTURE, stepper.REED, ctx=JobContext(job_id="reed:mock", source="reed", budget_max_lpa=35000))
    assert out.status == "applied", (out.status, out.detail, out.missing, out.notes)
    assert page.evaluate("window.sent") == {"rtw": "Yes", "sal": "34000"}
    assert page.evaluate("window.promo") == "×"            # closed, not subscribed


def test_stepper_dry_run_stops_at_submit(page, answer_bank, monkeypatch):
    monkeypatch.setattr(values, "_home_country", lambda: "GB")
    answer_bank.update(expected_salary=34000, salary_unit="annual", current_salary=28000, work_authorization="British citizen")
    job = {"id": "reed:mock", "url": FIXTURE.as_uri(), "source": "reed", "company": "Example Ltd", "title": "Junior Developer"}
    out = stepper.run(page, job, FIXTURE, stepper.REED, dry_run=True, ctx=JobContext(job_id="reed:mock", source="reed"))
    assert out.status == "dry_run"
    assert "application has been sent" not in page.inner_text("body").lower()


def test_reed_discovery(monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%d/%m/%Y")
    monkeypatch.setenv("REED_API_KEY", "test")
    calls = []

    def fake_get(path, key):
        calls.append(path)
        if path.startswith("/search"):
            return {"results": [
                {"jobId": 1, "jobTitle": "Graduate Software Engineer", "employerName": "Example Ltd", "locationName": "London",
                 "minimumSalary": 30000, "maximumSalary": 35000, "date": today, "jobUrl": "https://www.reed.co.uk/jobs/1"},
                {"jobId": 2, "jobTitle": "Sales Executive", "employerName": "Other", "locationName": "London", "date": today},
                {"jobId": 3, "jobTitle": "Graduate Developer", "employerName": "Old Co", "locationName": "Leeds", "date": "01/01/2020"},
            ]}
        return {"jobDescription": "<p>Build things in <b>Python</b>.</p>"}

    monkeypatch.setattr(reed, "_get", fake_get)
    out = list(reed.discover([{"keywords": "graduate software engineer", "location": "London"}],
                             known=lambda i: False, title_ok=lambda t: "Sales" not in t, log=lambda m: None))
    assert [p.external_id for p in out] == ["1"]
    assert out[0].description.startswith("Salary: £30,000 - £35,000 per annum")
    assert "Build things in Python" in out[0].description
