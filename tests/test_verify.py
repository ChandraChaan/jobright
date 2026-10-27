"""Rule checks and fallback, run against the real master profile without the API."""

from jobright.profile import load_profile
from jobright.render import tailored_view, to_html
from jobright.tailor import Line, SkillGroup, TailoredResume, TailoredRole
from jobright.verify import fall_back_to_sources, rule_issues

profile = load_profile()


def _resume(**overrides) -> TailoredResume:
    data = dict(
        headline="Principal Software Engineer | Mobile Architecture",
        summary=[Line(text="Principal Software Engineer with 8+ years across FinTech and EdTech.",
                      source_ids=["s1"])],
        skills=[SkillGroup(name="Mobile", items=["Flutter", "Dart"])],
        roles=[TailoredRole(role_id=r.id, bullets=[Line(text=r.bullets[0].text, source_ids=[r.bullets[0].id])],
                            project_ids=[]) for r in profile.roles],
        highlights=[],
        other_project_ids=[],
        keywords_used=[],
        gaps=[],
    )
    data.update(overrides)
    return TailoredResume(**data)


def test_clean_resume_passes():
    assert rule_issues(profile, _resume()) == []


def test_invented_number_is_caught():
    bad = _resume(summary=[Line(text="Led a team of 25 engineers.", source_ids=["h2"])])
    issues = rule_issues(profile, bad)
    assert any("25" in i.problem for i in issues)


def test_unknown_skill_and_missing_role_are_caught():
    bad = _resume(skills=[SkillGroup(name="Mobile", items=["Flutter", "React Native"])])
    bad.roles = bad.roles[:-1]
    problems = " ".join(i.problem for i in rule_issues(profile, bad))
    assert "skill is not in the master profile" in problems
    assert "role missing" in problems


def test_fallback_restores_source_text_and_roles():
    bad = _resume(summary=[Line(text="Led a team of 25 engineers.", source_ids=["h2"])],
                  skills=[SkillGroup(name="Mobile", items=["Flutter", "React Native"])])
    bad.roles = bad.roles[1:]
    fixed = fall_back_to_sources(profile, bad, rule_issues(profile, bad))
    assert rule_issues(profile, fixed) == []
    assert fixed.summary[0].text == profile.facts()["h2"]
    assert [r.role_id for r in fixed.roles] == [r.id for r in profile.roles]


def test_render_uses_profile_titles_and_dates():
    html = to_html(tailored_view(profile, _resume()))
    assert "Principal Software Engineer (Mobile)" in html
    assert "Apr 2026 — Present" in html
    assert "Mar 2018 — Jan 2021" in html
