"""Resume → profile → career plan, without calling Claude."""

import yaml

from jobright import config, onboard
from jobright.onboard import CareerPlan, Strength, TrackPlan, XEducation, XProfile, XRole


def _x() -> XProfile:
    return XProfile(
        name="Sam Patel", location="London, United Kingdom", email="sam@example.com", phone="+44 7700 900123",
        titles=["Computer Science Graduate"], years_experience="0 (graduate)",
        summary=["Computer Science graduate who builds web apps with React and Python."],
        skills=[{"name": "Languages", "items": ["Python", "JavaScript", "SQL"]}, {"name": "Frameworks", "items": ["React", "Django"]}],
        roles=[XRole(title="Software Engineering Intern", company="Example Bank", engagement="Internship",
                     start="2025-06", end="2025-09", bullets=["Built an internal dashboard in React used by the payments team."])],
        education=[XEducation(degree="BSc Computer Science", institution="University of Leeds", end="2026", grade="2:1",
                              details=["Dissertation on recommender systems in Python."])],
    )


def test_profile_gets_stable_fact_ids_and_education_details():
    p = onboard.to_profile(_x())
    facts = p.facts()
    assert p.roles[0].id == "example-bank" and p.roles[0].bullets[0].id in facts
    assert any("Dissertation" in t for t in facts.values())
    assert p.education[0].grade == "2:1"


def test_unfounded_flags_invented_lines():
    resume = ("Sam Patel. Computer Science graduate who builds web apps with React and Python. Software Engineering Intern, "
              "Example Bank: built an internal dashboard in React used by the payments team. BSc Computer Science, "
              "University of Leeds, 2:1. Dissertation on recommender systems in Python.")
    p = onboard.to_profile(_x())
    assert onboard.unfounded(p, resume) == []
    p.highlights.append(type(p.summary[0])(id="h9", text="Led a Kubernetes migration across 40 microservices."))
    assert [fid for fid, _ in onboard.unfounded(p, resume)] == ["h9"]


def test_plan_becomes_tracks_and_searches(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.yaml")
    config.settings.cache_clear()
    plan = CareerPlan(level="graduate", years=0.3, primary="Web development", summary="…",
                      strengths=[Strength(skill="React", depth="working", evidence="internship")],
                      transferable=[], growth_areas=["Cloud"], title_exclude=["Senior", "Lead"],
                      tracks=[TrackPlan(name="graduate_software", label="Graduate software engineer",
                                        titles=["Graduate Software Engineer", "Junior Developer"],
                                        search_keywords=["graduate software engineer", "junior developer"],
                                        weight=1.0, min_fit=50, stretch=False, why="core")])
    try:
        onboard.apply_plan(plan, "GB", ["London"], ["linkedin", "reed", "ats"], ["Example Bank"])
        s = config.settings()
        assert s["profile"]["country"] == "GB"
        assert "graduate_software" in s["tracks"] and "Senior" in s["targets"]["title_exclude"]
        assert {"London", "United Kingdom", "Remote"} <= set(s["targets"]["locations"])
        li = s["searches"]["linkedin"]
        assert {"keywords": "graduate software engineer", "geo_id": 90009496, "label": "London"} in li
        assert s["searches"]["reed"][0] == {"keywords": "graduate software engineer", "location": "London"}
        assert yaml.safe_load((tmp_path / "settings.yaml").read_text())["exclude_companies"] == ["Example Bank"]
    finally:
        config.settings.cache_clear()
