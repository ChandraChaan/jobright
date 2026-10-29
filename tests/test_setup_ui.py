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
