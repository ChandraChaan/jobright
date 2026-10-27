"""Shared fixtures. Tests run against a fictional user (tests/fixtures/home), never
your own data: JOBRIGHT_HOME points at a temporary copy before jobright loads."""

import os
import shutil
import tempfile
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="jobright-test-"))
shutil.copytree(Path(__file__).parent / "fixtures" / "home", _HOME, dirs_exist_ok=True)
os.environ["JOBRIGHT_HOME"] = str(_HOME)
os.environ["JOBRIGHT_OFFLINE"] = "1"
os.environ.pop("JOBRIGHT_DB", None)

import pytest  # noqa: E402

from jobright import answers  # noqa: E402

BANK = {
    "total_experience_years": 6.8,
    "relevant_experience_years": {"Flutter": 6, "Mobile development": 6, "Android": 5, "iOS": 4, "Banking": None},
    "current_salary": 30,
    "expected_salary": 40,
    "salary_unit": "lpa",
    "currency": "INR",
    "notice_period_days": 14,
    "currently_serving_notice": False,
    "current_location": "Bengaluru",
    "preferred_locations": ["Bengaluru", "Hyderabad"],
    "willing_to_relocate": True,
    "open_to_onsite": True,
    "open_to_hybrid": True,
    "open_to_remote": True,
    "work_authorization": "Indian citizen",
    "needs_visa_sponsorship_abroad": True,
    "gender": "female",
    "highest_education": "B.Tech, Computer Science",
    "graduation_year": 2017,
    "english_proficiency": None,
    "has_passport": None,
}


@pytest.fixture(autouse=True)
def answer_bank(monkeypatch, tmp_path):
    bank = dict(BANK)
    monkeypatch.setattr(answers, "bank", lambda: bank)
    monkeypatch.setattr(answers, "SAVED_PATH", tmp_path / "saved.yaml")
    monkeypatch.setattr(answers, "PENDING_PATH", tmp_path / "pending.yaml")
    monkeypatch.setattr(answers, "FIELD_CACHE_PATH", tmp_path / "cache.yaml")
    return bank
