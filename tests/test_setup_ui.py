"""Every setup-wizard page renders, and the answers form writes a valid answer bank."""

import pytest
import yaml
from fastapi.testclient import TestClient

from jobright import config
from jobright.dashboard import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.yaml")
    return TestClient(app)


@pytest.mark.parametrize("path", ["/setup", "/setup/key", "/setup/resume", "/setup/profile", "/setup/country",
                                  "/setup/plan", "/setup/answers", "/setup/logins", "/setup/finish", "/", "/questions"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_steps_are_marked(client):
    html = client.get("/setup/country").text
    assert 'class="done ' in html or "done" in html
    assert "3 · Country" in html


def test_answers_form_writes_bank(client):
    form = {"total_experience_years": "8.5", "current_salary": "36", "expected_salary": "45", "notice_period_days": "30",
            "willing_to_relocate": "yes", "work_authorization": "Indian citizen", "skillname::0": "Flutter",
            "skillyears::0": "7", "skillname::1": "", "skillyears::1": ""}
    r = client.post("/setup/answers", data=form, follow_redirects=False)
    assert r.status_code == 303
    bank = yaml.safe_load(config.ANSWERS_PATH.read_text())
    assert bank["total_experience_years"] == 8.5 and bank["current_salary"] == 36
    assert bank["willing_to_relocate"] is True and bank["salary_unit"] == "lpa"
    assert bank["relevant_experience_years"] == {"Flutter": 7.0}
    assert bank["gender"] is None                      # left empty: asked when a form needs it


def test_company_site_jobs_offer_applying_by_hand_not_approve(client):
    from jobright import store
    assert store.add_job(source="linkedin", external_id="ext1", url="https://linkedin.example/job", title="Flutter Lead",
                         company="Byhand Co", apply_type="external")
    store.update_job("linkedin:ext1", status="needs_you",
                     reason="company site the bot can't fill: https://careers.byhand.example/job/1")
    html = client.get("/jobs/linkedin:ext1").text
    assert 'href="https://careers.byhand.example/job/1"' in html and "I applied, mark applied" in html
    assert "Approve and apply now" not in html
    client.post("/jobs/linkedin:ext1/status", data={"status": "applied"})
    assert store.get_job("linkedin:ext1")["applied_at"]


def test_blocking_questions_are_answered_on_the_job_page_then_applied(client, monkeypatch):
    import json
    from jobright import answers, dashboard, store
    spawned = []
    monkeypatch.setattr(dashboard, "_spawn", lambda *a: spawned.append(a))
    monkeypatch.setattr(dashboard, "_running", lambda: False)
    answers.add_pending("Are you ok with the Remote short time contract role", "radio", ["Yes", "No"], "linkedin:q1")
    assert store.add_job(source="linkedin", external_id="q1", url="u", title="Mobile Developer", company="Askme Co",
                         apply_type="easy_apply")
    store.update_job("linkedin:q1", status="needs_you", reason="Easy Apply asked questions only you can answer",
                     missing_json=json.dumps(["Are you ok with the Remote short time contract role",
                                              "Expected hourly rate (site says: This field is required)"]))
    html = client.get("/jobs/linkedin:q1").text
    assert 'name="q::Are you ok with the Remote short time contract role"' in html and "<option >Yes</option>" in html
    assert 'name="q::Expected hourly rate"' in html and "site says: This field is required" in html
    client.post("/jobs/linkedin:q1/answers", data={"q::Are you ok with the Remote short time contract role": "No",
                                                    "q::Expected hourly rate": "20"})
    assert answers.saved()[answers.norm_q("Expected hourly rate")] == "20"
    assert answers.pending_questions() == []
    assert store.get_job("linkedin:q1")["status"] == "approved"
    assert spawned == [("apply", "--job", "linkedin:q1")]


def test_saving_a_note_keeps_the_status(client):
    from jobright import store
    assert store.add_job(source="linkedin", external_id="n1", url="u", title="Flutter Dev", company="Noteco")
    store.update_job("linkedin:n1", status="approved")
    assert '<option value="" selected>approved (unchanged)</option>' in client.get("/jobs/linkedin:n1").text
    client.post("/jobs/linkedin:n1/status", data={"status": "", "notes": "recruiter called"})
    job = store.get_job("linkedin:n1")
    assert job["status"] == "approved" and job["notes"] == "recruiter called"
