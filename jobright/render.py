"""Turn a resume into HTML and an A4 PDF. Titles, companies and dates always
come from the master profile, never from the model."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from jobright.config import TEMPLATES_DIR
from jobright.profile import Profile, Role
from jobright.tailor import TailoredResume

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html", "j2"]))


def _month(ym: str) -> str:
    year, month = ym.split("-")
    return f"{_MONTHS[int(month) - 1]} {year}"


def _dates(role: Role) -> str:
    return f"{_month(role.start)} — {_month(role.end) if role.end else 'Present'}"


def _role_view(role: Role, bullets: list[str], project_ids: list[str]) -> dict:
    # A role built around one product already describes it in its bullets.
    projects = {p.id: p for p in role.projects} if len(role.projects) > 1 else {}
    return {
        "title": role.title,
        "company": role.company,
        "engagement": role.engagement,
        "dates": _dates(role),
        "bullets": bullets,
        "projects": [projects[p] for p in project_ids if p in projects],
    }


def tailored_view(profile: Profile, resume: TailoredResume) -> dict:
    others = {p.id: p for p in profile.other_projects}
    roles = []
    for tr in resume.roles:
        role = profile.role(tr.role_id)
        if role:
            roles.append(_role_view(role, [l.text for l in tr.bullets], tr.project_ids))
    return {
        "contact": profile.contact,
        "headline": resume.headline,
        "summary": [l.text for l in resume.summary],
        "skills": [g.model_dump() for g in resume.skills],
        "roles": roles,
        "highlights": [l.text for l in resume.highlights],
        "other_projects": [others[p] for p in resume.other_project_ids if p in others],
        "education": profile.education,
    }


def base_view(profile: Profile) -> dict:
    """The full master resume: every fact, no tailoring."""
    return {
        "contact": profile.contact,
        "headline": " | ".join(profile.titles),
        "summary": [f.text for f in profile.summary],
        "skills": [{"name": k, "items": v} for k, v in profile.skills.items()],
        "roles": [_role_view(r, [b.text for b in r.bullets], [p.id for p in r.projects])
                  for r in profile.roles],
        "highlights": [f.text for f in [*profile.highlights, *profile.practices]],
        "other_projects": profile.other_projects,
        "education": profile.education,
    }


def to_html(view: dict) -> str:
    return _env.get_template("resume.html.j2").render(**view)


def plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.split("<body>", 1)[-1])).strip()


def write_pdf(html: str, path: Path) -> int:
    """Write the PDF and return its page count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        browser.close()
    path.write_bytes(pdf)
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))
