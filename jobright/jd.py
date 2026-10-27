"""Read a job description: requirements, ATS keywords and fit against the profile."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from jobright import config
from jobright.llm import LLM
from jobright.profile import Profile


class JobAnalysis(BaseModel):
    title: str
    company: str
    location: str
    seniority: str = Field(description="e.g. Senior, Lead, Staff, Principal, Architect")
    years_required: str = Field(description="As stated, e.g. '8+ years'; 'not stated' if absent")
    must_have: list[str] = Field(description="Hard requirements, short phrases")
    nice_to_have: list[str]
    keywords: list[str] = Field(
        description="10-25 ATS terms a recruiter would search for, as written in the JD")
    required_skills: list[str] = Field(
        description="The concrete skills the JD requires (must-have): technologies, tools, practices, "
                    "each a short name such as 'Flutter', 'Riverpod', 'Unit testing', 'CI/CD'")
    preferred_skills: list[str] = Field(
        default_factory=list,
        description="Skills the JD lists as nice-to-have, preferred or a plus; short names")
    salary_text: str = Field(
        default="not stated",
        description="The salary or CTC range exactly as the JD states it, e.g. '₹25L - ₹40L/year'; 'not stated' if absent")
    responsibilities: list[str] = Field(description="The 3-8 main responsibilities")
    domain: str
    fit_score: int = Field(ge=0, le=100)
    fit_reason: str = Field(description="2-4 sentences: strongest matches and real gaps")
    gaps: list[str] = Field(description="Requirements the profile does not show")
    recommendation: Literal["apply", "maybe", "skip"]
    role_family: Literal["mobile", "architecture", "leadership", "ai_applications", "fullstack",
                         "backend_cloud", "data_ml", "other"] = Field(
        default="other", description="What the role mainly is, whatever its title says")
    realistic: bool = Field(
        default=True,
        description="Could this candidate do this job well within the first months, judged from what "
                    "the profile shows, including transferable skills? False when a core requirement "
                    "is missing or the level is clearly out of reach")
    realism_reason: str = Field(default="", description="One or two sentences backing `realistic`")
    growth_score: int = Field(
        default=50, ge=0, le=100,
        description="Career value of the role for this candidate: scope and seniority step, demand for the "
                    "skills it builds, company and domain trajectory")


def skill_match(analysis: JobAnalysis, profile_text: str) -> tuple[int, list[str], list[str]]:
    """Weighted percent of the JD's skills that appear in the master profile:
    a must-have skill counts twice a nice-to-have one.

    Counted in code, not judged by the model, so the apply threshold is
    predictable: the same JD and profile always give the same number.
    """
    from jobright.coverage import keyword_coverage

    must = analysis.required_skills or analysis.keywords
    nice = [s for s in analysis.preferred_skills if s.lower() not in {m.lower() for m in must}]
    have_m, miss_m = keyword_coverage(must, profile_text)
    have_n, miss_n = keyword_coverage(nice, profile_text)
    total = 2 * len(must) + len(nice)
    pct = round(100 * (2 * len(have_m) + len(have_n)) / total) if total else 0
    return pct, have_m + have_n, miss_m + [f"{s} (nice to have)" for s in miss_n]


def years_band(*texts: str) -> tuple[float | None, float | None]:
    """Experience the JD asks for, as (min, max) years: '3-5 Yrs' → (3, 5), '7+ years' → (7, None)."""
    for text in texts:
        t = (text or "").lower()
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", t)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = re.search(r"(?:minimum|min\.?|at least|over)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", t) \
            or re.search(r"(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)", t) \
            or re.search(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:of\s*)?(?:\w+\s*){0,4}experience", t)
        if m:
            return float(m.group(1)), None
    return None, None


def _career_context() -> str:
    """The user's career plan (level, strengths with depth), when setup made one."""
    if not config.CAREER_PATH.exists():
        return ""
    import yaml
    plan = yaml.safe_load(config.CAREER_PATH.read_text()) or {}
    keep = {k: plan.get(k) for k in ("level", "years", "primary", "summary", "strengths", "transferable")}
    return "\n<career_plan>\n" + yaml.safe_dump(keep, sort_keys=False, allow_unicode=True) + "</career_plan>"


def analyze(llm: LLM, profile: Profile, jd_text: str, deep: bool = False) -> JobAnalysis:
    """First read with the fast model; `deep` asks the stronger model for a
    second opinion (used before tailoring for roles outside your core track)."""
    models = config.settings()["models"]
    return llm.structured(
        purpose="deep_fit" if deep else "analyze",
        model=models.get("deep_fit", models["analyze"]) if deep else models["analyze"],
        instructions=config.prompt("analyze"),
        context="<master_profile>\n" + profile.for_model() + "</master_profile>" + _career_context(),
        prompt="<job_description>\n" + jd_text.strip() + "\n</job_description>",
        schema=JobAnalysis,
        effort="high" if deep else "medium",
        max_tokens=12000 if deep else 8000,
    )
