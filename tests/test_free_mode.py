"""Free and local engines: everything works with no API key, and stays honest."""

import io
import json
from pathlib import Path

import pytest

from jobright import config, engine, heuristics, learn, store
from jobright.llm import LLM, LLMError
from jobright.onboard import to_profile
from jobright.profile import load_profile

FLUTTER_JD = """Senior Flutter Developer
Requirements:
- 5+ years building mobile apps, 3+ with Flutter and Dart
- Firebase, REST APIs, state management (BLoC or Provider)
- Published apps on the App Store and Google Play
Nice to have: Kotlin, Swift, CI/CD
"""
GRAD_CV = (Path(__file__).parent / "fixtures" / "grad_resume.txt").read_text()


@pytest.fixture
def no_offline(monkeypatch):
    monkeypatch.delenv("JOBRIGHT_OFFLINE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return monkeypatch


def test_engine_falls_back_without_key_or_ollama(no_offline):
    no_offline.setattr(engine, "ollama_models", lambda: ())
    assert engine.current() == "free" and not engine.uses_ai() and not engine.rewrites_resumes()
    no_offline.setattr(engine, "ollama_models", lambda: ("qwen2.5:7b",))
    assert engine.current() == "local" and engine.uses_ai() and not engine.rewrites_resumes()
    no_offline.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert engine.current() == "claude" and engine.rewrites_resumes()


def test_offline_forces_free_and_llm_refuses():
    assert engine.current() == "free"
    with pytest.raises(LLMError):
        LLM()


def test_rules_read_a_matching_job():
    a = heuristics.analyze(load_profile(), FLUTTER_JD, title="Senior Flutter Developer", company="Acme")
    assert "Flutter" in a.required_skills and "Firebase" in a.required_skills
    assert "Kotlin" in a.preferred_skills and "Kotlin" not in a.required_skills
    assert a.fit_score >= 70 and a.recommendation == "apply"
    assert a.years_required.startswith("5")


def test_rules_mark_senior_roles_down_for_a_graduate(answer_bank):
    answer_bank["total_experience_years"] = 0
    grad = to_profile(heuristics.parse_resume(GRAD_CV))
    a = heuristics.analyze(grad, "Senior Backend Engineer\nRequirements: 7+ years of Python, Django, AWS.",
                           title="Senior Backend Engineer")
    assert a.fit_score < 55 and a.recommendation == "skip"


def test_free_resume_uses_only_your_own_lines():
    p = load_profile()
    facts = p.facts()
    r = heuristics.tailor(p, heuristics.analyze(p, FLUTTER_JD, title="Senior Flutter Developer"))
    lines = [*r.summary, *r.highlights, *[l for role in r.roles for l in role.bullets]]
    assert lines
    for line in lines:
        assert len(line.source_ids) == 1 and facts[line.source_ids[0]] == line.text   # word for word
    assert "Flutter" in r.keywords_used


def test_resume_import_without_ai():
    x = heuristics.parse_resume(GRAD_CV)
    assert (x.name, x.location, x.email) == ("Sam Patel", "London, United Kingdom", "sam.patel@example.com")
    assert x.titles == ["BSc Computer Science Graduate"] and x.years_experience == "0"
    assert not any("@" in s for s in x.summary)            # contact line is not a summary sentence
    intern = next(r for r in x.roles if "Intern" in r.title)
    assert (intern.company, intern.start, intern.end, intern.engagement) == ("Example Bank, London", "2024-06", "2024-09", "Internship")
    assert len(intern.bullets) == 3
    assert x.education[0].grade == "First Class Honours" and x.education[0].end == "2025"
    assert {g.name for g in x.skills} == {"Languages", "Frameworks", "Tools"}


def test_rule_plan_for_a_graduate():
    plan = heuristics.plan_career(to_profile(heuristics.parse_resume(GRAD_CV)), "United Kingdom")
    names = [t.name for t in plan.tracks]
    assert plan.level == "graduate" and names[0] == "graduate"
    assert "backend" in names and "Senior" in plan.title_exclude
    backend = next(t for t in plan.tracks if t.name == "backend")
    assert "JavaScript" not in backend.titles               # "java" must not match JavaScript


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_local_model_retries_bad_json(monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    calls = []

    def fake(req, timeout=0):
        body = json.loads(req.data)
        calls.append(body)
        assert body["format"]["properties"]["answer"]
        content = "{oops" if len(calls) == 1 else '{"answer": "yes"}'
        return _Reply(json.dumps({"message": {"content": content}, "prompt_eval_count": 10, "eval_count": 3}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake)
    out = LLM(provider="local").structured(purpose="test", model="ignored", instructions="i", context="c",
                                          prompt="p", schema=Out)
    assert out.answer == "yes" and len(calls) == 2
    assert "did not match" in calls[1]["messages"][-1]["content"]


def test_learning_needs_evidence_and_is_bounded():
    few = {"mobile": {"applied": 5, "interview": 5, "offer": 0, "rejected": 0}}
    assert learn.factors(few) == {}
    data = {"mobile": {"applied": 10, "interview": 4, "offer": 1, "rejected": 2},
            "backend": {"applied": 10, "interview": 0, "offer": 0, "rejected": 6},
            "cloud": {"applied": 3, "interview": 0, "offer": 0, "rejected": 1}}
    f = learn.factors(data)
    assert f["mobile"] == 1 + learn.BOUND and f["backend"] == 1 - learn.BOUND
    assert "cloud" not in f                                 # too few to judge


def test_recent_rejection_blocks_reapplying():
    from jobright.apply.dispatch import precheck
    assert store.add_job(source="linkedin", external_id="r1", url="u", title="Flutter Developer", company="Rejecto Ltd")
    store.update_job("linkedin:r1", status="rejected", applied_at="2026-01-01T00:00:00")
    assert store.add_job(source="naukri", external_id="r2", url="u", title="Mobile Architect", company="Rejecto")
    out = precheck(store.get_job("naukri:r2"))
    assert out and out.status == "skipped" and "rejected" in out.detail


def test_track_outcomes_count_each_application_once():
    for i, status in enumerate(("applied", "applied", "interview")):
        assert store.add_job(source="linkedin", external_id=f"t{i}", url="u", title=f"Role {i}", company=f"Countco {i}")
        store.update_job(f"linkedin:t{i}", status=status, track="countmobile", applied_at="2026-01-01T00:00:00")
    assert store.track_outcomes()["countmobile"] == {"applied": 3, "interview": 1, "offer": 0, "rejected": 0}
