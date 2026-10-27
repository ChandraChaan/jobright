"""The master profile: every fact a resume may contain, each with an id."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from jobright.config import PROFILE_PATH


class Fact(BaseModel):
    id: str
    text: str


class Link(BaseModel):
    label: str
    url: str


class Contact(BaseModel):
    name: str
    location: str
    email: str
    phone: str
    links: list[Link]


class Project(BaseModel):
    id: str
    name: str
    summary: str
    details: list[Fact] = []


class Role(BaseModel):
    id: str
    title: str
    company: str
    engagement: str | None = None
    start: str  # YYYY-MM
    end: str | None = None  # None while current
    bullets: list[Fact]
    projects: list[Project] = []


class Education(BaseModel):
    degree: str
    institution: str
    location: str | None = None
    end: str | None = None          # YYYY-MM or YYYY
    grade: str | None = None        # e.g. "First Class Honours", "2:1", "CGPA 8.2/10"
    details: list[Fact] = []        # modules, dissertation, awards


class Profile(BaseModel):
    contact: Contact
    titles: list[str]
    years_experience: str
    summary: list[Fact]
    highlights: list[Fact]
    practices: list[Fact] = []
    skills: dict[str, list[str]]
    roles: list[Role]
    other_projects: list[Project] = []
    education: list[Education]

    def facts(self) -> dict[str, str]:
        """Every citable fact by id. A project's id cites its summary."""
        out: dict[str, str] = {}
        for f in [*self.summary, *self.highlights, *self.practices]:
            out[f.id] = f.text
        for role in self.roles:
            for f in role.bullets:
                out[f.id] = f.text
            for p in role.projects:
                out[p.id] = f"{p.name}: {p.summary}"
                for f in p.details:
                    out[f.id] = f"{p.name}: {f.text}"
        for p in self.other_projects:
            out[p.id] = f"{p.name}: {p.summary}"
            for f in p.details:
                out[f.id] = f"{p.name}: {f.text}"
        for e in self.education:
            for f in e.details:
                out[f.id] = f"{e.degree}, {e.institution}: {f.text}"
        return out

    def skill_names(self) -> set[str]:
        return {s.lower() for items in self.skills.values() for s in items}

    def role(self, role_id: str) -> Role | None:
        return next((r for r in self.roles if r.id == role_id), None)

    def for_model(self) -> str:
        """Profile as sent to Claude: everything except contact details."""
        data = self.model_dump(exclude={"contact"}, exclude_none=True)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    with path.open() as f:
        profile = Profile.model_validate(yaml.safe_load(f))
    ids = [fid for fid in _all_ids(profile)]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate fact ids in {path.name}: {sorted(dupes)}")
    return profile


def _all_ids(profile: Profile):
    yield from (f.id for f in profile.summary)
    yield from (f.id for f in profile.highlights)
    yield from (f.id for f in profile.practices)
    for role in profile.roles:
        yield role.id
        yield from (f.id for f in role.bullets)
        for p in role.projects:
            yield p.id
            yield from (f.id for f in p.details)
    for p in profile.other_projects:
        yield p.id
        yield from (f.id for f in p.details)
    for e in profile.education:
        yield from (f.id for f in e.details)
