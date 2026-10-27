"""Write a resume for one job from the master profile, citing every source fact."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from jobright import config
from jobright.jd import JobAnalysis
from jobright.llm import LLM
from jobright.profile import Profile


class Line(BaseModel):
    text: str
    source_ids: list[str] = Field(description="Ids of the profile facts this line restates")


class SkillGroup(BaseModel):
    name: str
    items: list[str] = Field(description="Skill names copied exactly from the profile's skills")


class TailoredRole(BaseModel):
    role_id: str
    bullets: list[Line]
    project_ids: list[str] = Field(description="Projects of this role to list, most relevant first")


class TailoredResume(BaseModel):
    headline: str
    summary: list[Line]
    skills: list[SkillGroup]
    roles: list[TailoredRole] = Field(description="Every role in the profile, newest first")
    highlights: list[Line]
    other_project_ids: list[str] = Field(description="0-2 ids from other_projects, only if relevant")
    keywords_used: list[str] = Field(description="JD keywords the resume now covers truthfully")
    gaps: list[str] = Field(description="JD requirements the profile cannot honestly cover")


def _job_block(analysis: JobAnalysis, jd_text: str) -> str:
    return (
        "<job_analysis>\n" + analysis.model_dump_json(indent=2) + "\n</job_analysis>\n"
        "<job_description>\n" + jd_text.strip() + "\n</job_description>"
    )


def tailor(llm: LLM, profile: Profile, analysis: JobAnalysis, jd_text: str) -> TailoredResume:
    return llm.structured(
        purpose="tailor",
        model=config.settings()["models"]["tailor"],
        instructions=config.prompt("tailor"),
        context="<master_profile>\n" + profile.for_model() + "</master_profile>",
        prompt=_job_block(analysis, jd_text) + "\n\nWrite the tailored resume.",
        schema=TailoredResume,
        effort="high",
    )


def revise(llm: LLM, profile: Profile, analysis: JobAnalysis, jd_text: str,
           draft: TailoredResume, issues: list[str]) -> TailoredResume:
    """Fix the lines the verifier rejected; keep everything else as it was."""
    problems = "\n".join(f"- {i}" for i in issues)
    return llm.structured(
        purpose="revise",
        model=config.settings()["models"]["tailor"],
        instructions=config.prompt("tailor"),
        context="<master_profile>\n" + profile.for_model() + "</master_profile>",
        prompt=(
            _job_block(analysis, jd_text)
            + "\n<draft>\n" + json.dumps(draft.model_dump(), indent=2) + "\n</draft>\n"
            + "<verifier_issues>\n" + problems + "\n</verifier_issues>\n\n"
            "A fact check rejected the lines above. Return the full resume with each "
            "rejected line rewritten so its cited facts support every word of it, or "
            "removed if no fact can. Leave lines that were not rejected unchanged."
        ),
        schema=TailoredResume,
        effort="high",
    )
