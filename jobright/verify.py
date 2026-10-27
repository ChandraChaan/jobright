"""Fact check a tailored resume against the master profile.

Two layers. Rule checks are exact and free: cited ids exist, numbers appear in
the cited facts, skills come from the profile, every role is present. Then a
second model reads each line next to its cited facts and flags anything they do
not support. A line that still fails after one revision is replaced with its
source fact verbatim, so the final resume never carries an unverified claim.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from jobright import config
from jobright.llm import LLM
from jobright.profile import Profile
from jobright.tailor import Line, TailoredResume, TailoredRole

_NUMBER = re.compile(r"\d[\d,.]*\s*(?:%|\+|k\b|m\b|lakh|cr)?", re.IGNORECASE)


class Issue(BaseModel):
    where: str  # e.g. "summary[1]", "roles.acme.bullets[2]", "skills"
    text: str
    problem: str


class LineVerdict(BaseModel):
    where: str
    verdict: Literal["supported", "overstated", "unsupported"]
    problem: str  # empty when supported


class Verification(BaseModel):
    verdicts: list[LineVerdict]


def _lines(resume: TailoredResume):
    for i, line in enumerate(resume.summary):
        yield f"summary[{i}]", line
    for role in resume.roles:
        for i, line in enumerate(role.bullets):
            yield f"roles.{role.role_id}.bullets[{i}]", line
    for i, line in enumerate(resume.highlights):
        yield f"highlights[{i}]", line


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", m.group(0)).lower().rstrip(".") for m in _NUMBER.finditer(text)}


def rule_issues(profile: Profile, resume: TailoredResume) -> list[Issue]:
    facts = profile.facts()
    issues: list[Issue] = []

    for where, line in _lines(resume):
        unknown = [s for s in line.source_ids if s not in facts]
        if not line.source_ids or unknown:
            issues.append(Issue(where=where, text=line.text,
                                problem=f"cites no valid fact (unknown ids: {unknown})"))
            continue
        cited = " ".join(facts[s] for s in line.source_ids)
        missing = _numbers(line.text) - _numbers(cited)
        if missing:
            issues.append(Issue(where=where, text=line.text,
                                problem=f"numbers {sorted(missing)} are not in the cited facts"))

    # A bullet under an employer may only restate what was done there.
    for role in resume.roles:
        source = profile.role(role.role_id)
        if source is None:
            continue
        own = {b.id for b in source.bullets}
        for p in source.projects:
            own |= {p.id, *(d.id for d in p.details)}
        for i, line in enumerate(role.bullets):
            foreign = [s for s in line.source_ids if s in facts and s not in own]
            if foreign:
                issues.append(Issue(where=f"roles.{role.role_id}.bullets[{i}]", text=line.text,
                                    problem=f"cites facts from outside this role: {foreign}"))

    known_skills = profile.skill_names()
    for group in resume.skills:
        for item in group.items:
            if item.lower() not in known_skills:
                issues.append(Issue(where=f"skills.{group.name}", text=item,
                                    problem="skill is not in the master profile"))

    profile_roles = [r.id for r in profile.roles]
    got = [r.role_id for r in resume.roles]
    for rid in profile_roles:
        if rid not in got:
            issues.append(Issue(where="roles", text=rid, problem="role missing; every role must stay"))
    for role in resume.roles:
        source = profile.role(role.role_id)
        if source is None:
            issues.append(Issue(where="roles", text=role.role_id, problem="unknown role id"))
            continue
        own = {p.id for p in source.projects}
        for pid in role.project_ids:
            if pid not in own:
                issues.append(Issue(where=f"roles.{role.role_id}.projects", text=pid,
                                    problem="project does not belong to this role"))

    others = {p.id for p in profile.other_projects}
    for pid in resume.other_project_ids:
        if pid not in others:
            issues.append(Issue(where="other_projects", text=pid, problem="unknown project id"))

    if not any(t.lower() in resume.headline.lower() for t in profile.titles):
        issues.append(Issue(where="headline", text=resume.headline,
                            problem=f"headline must include a title actually held: {profile.titles}"))
    return issues


def model_issues(llm: LLM, profile: Profile, resume: TailoredResume) -> list[Issue]:
    facts = profile.facts()
    items = []
    for where, line in _lines(resume):
        cited = "\n".join(f"  [{s}] {facts[s]}" for s in line.source_ids if s in facts)
        items.append(f"<line where=\"{where}\">\n  {line.text}\n cited facts:\n{cited}\n</line>")
    result = llm.structured(
        purpose="verify",
        model=config.settings()["models"]["verify"],
        instructions=config.prompt("verify"),
        context="<master_profile>\n" + profile.for_model() + "</master_profile>",
        prompt="<headline>" + resume.headline + "</headline>\n" + "\n".join(items),
        schema=Verification,
        effort="high",
    )
    texts = {"headline": resume.headline, **{w: l.text for w, l in _lines(resume)}}
    return [
        Issue(where=v.where, text=texts.get(v.where, ""), problem=f"{v.verdict}: {v.problem}")
        for v in result.verdicts
        if v.verdict != "supported"
    ]


def fall_back_to_sources(profile: Profile, resume: TailoredResume,
                         issues: list[Issue]) -> TailoredResume:
    """Replace each still-failing line with its first cited fact, verbatim.

    Lines with no valid citation are dropped, as are unknown skills and
    projects. Roles the model left out are restored with their source bullets.
    """
    facts = profile.facts()
    bad = {i.where for i in issues}
    bad_skills = {i.text.lower() for i in issues if i.where.startswith("skills.")}
    bad_projects = {i.text for i in issues if i.where.endswith("projects") or i.where == "other_projects"}

    def fix(where: str, line: Line) -> Line | None:
        if where not in bad:
            return line
        valid = [s for s in line.source_ids if s in facts]
        return Line(text=facts[valid[0]], source_ids=valid[:1]) if valid else None

    def fix_all(prefix: str, lines: list[Line]) -> list[Line]:
        fixed = (fix(f"{prefix}[{i}]", line) for i, line in enumerate(lines))
        return [line for line in fixed if line]

    data = resume.model_copy(deep=True)
    data.summary = fix_all("summary", data.summary)
    data.highlights = fix_all("highlights", data.highlights)
    for role in data.roles:
        role.bullets = fix_all(f"roles.{role.role_id}.bullets", role.bullets)
        role.project_ids = [p for p in role.project_ids if p not in bad_projects]
    seen: set[str] = set()
    data.roles = [r for r in data.roles
                  if profile.role(r.role_id) and not (r.role_id in seen or seen.add(r.role_id))]
    for source in profile.roles:
        if source.id not in seen:
            data.roles.append(TailoredRole(
                role_id=source.id,
                bullets=[Line(text=b.text, source_ids=[b.id]) for b in source.bullets[:3]],
                project_ids=[p.id for p in source.projects[:2]],
            ))
    order = {r.id: i for i, r in enumerate(profile.roles)}
    data.roles.sort(key=lambda r: order[r.role_id])
    for group in data.skills:
        group.items = [s for s in group.items if s.lower() not in bad_skills]
    data.skills = [g for g in data.skills if g.items]
    data.other_project_ids = [p for p in data.other_project_ids if p not in bad_projects]
    if any(i.where == "headline" for i in issues):
        data.headline = " | ".join(profile.titles)
    return data
