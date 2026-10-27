"""One job description in, one verified tailored resume out."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jobright import config
from jobright.coverage import keyword_coverage
from jobright.jd import JobAnalysis, analyze
from jobright.llm import LLM
from jobright.profile import Profile
from jobright.render import base_view, plain_text, tailored_view, to_html, write_pdf
from jobright.tailor import TailoredResume, revise, tailor
from jobright.verify import Issue, fall_back_to_sources, model_issues, rule_issues


@dataclass
class Built:
    out_dir: Path
    pdf: Path
    report: dict


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40] or "unknown"


def profile_text(profile: Profile) -> str:
    """Plain text of the full master resume: the ceiling for any keyword check."""
    return plain_text(to_html(base_view(profile)))


def _shorter(resume: TailoredResume):
    """Successively shorter versions, least valuable content cut first.

    Roles are newest first, so a higher index is an older role.
    """
    def step(fn):
        nonlocal resume
        resume = resume.model_copy(deep=True)
        fn(resume)
        return resume

    def cap_roles(r, start, bullets=None, projects=None):
        for role in r.roles[start:]:
            if bullets is not None:
                role.bullets = role.bullets[:bullets]
            if projects is not None:
                role.project_ids = role.project_ids[:projects]

    yield step(lambda r: setattr(r, "other_project_ids", []))
    yield step(lambda r: setattr(r, "highlights", r.highlights[:3]))
    yield step(lambda r: cap_roles(r, 3, projects=0))
    yield step(lambda r: setattr(r, "summary", r.summary[:3]))
    yield step(lambda r: cap_roles(r, 2, bullets=2))
    yield step(lambda r: setattr(r, "highlights", r.highlights[:2]))
    yield step(lambda r: cap_roles(r, 0, projects=1))
    yield step(lambda r: cap_roles(r, 1, bullets=4))
    yield step(lambda r: setattr(r, "highlights", []))
    yield step(lambda r: cap_roles(r, 2, projects=0))


def _render_within(profile: Profile, resume: TailoredResume, pdf_path: Path,
                   max_pages: int) -> tuple[TailoredResume, str, int]:
    """Render, trimming lowest-value content until the PDF fits max_pages."""
    html = to_html(tailored_view(profile, resume))
    pages = write_pdf(html, pdf_path)
    for shorter in _shorter(resume):
        if pages <= max_pages:
            break
        resume = shorter
        html = to_html(tailored_view(profile, resume))
        pages = write_pdf(html, pdf_path)
    return resume, html, pages


def _check(llm: LLM, profile: Profile, resume: TailoredResume) -> list[Issue]:
    issues = rule_issues(profile, resume)
    return issues + model_issues(llm, profile, resume)


def job_dir(analysis: JobAnalysis) -> Path:
    name = slug(analysis.title) if analysis.company == "unknown" else f"{slug(analysis.company)}_{slug(analysis.title)}"
    out = config.APPLICATIONS_DIR / f"{date.today()}_{name}"
    n = 2
    while out.exists() and (out / "report.json").exists():
        out = out.with_name(f"{date.today()}_{name}_{n}")
        n += 1
    return out


def build_resume(profile: Profile, jd_text: str, *, llm: LLM | None = None,
                 analysis: JobAnalysis | None = None, log=print) -> Built:
    from jobright import engine, heuristics

    rewrite = engine.rewrites_resumes()
    if llm is None and engine.uses_ai():
        llm = LLM()

    if analysis is None:
        log("Reading the job description…")
        analysis = analyze(llm, profile, jd_text) if llm else heuristics.analyze(profile, jd_text)
        log(f"  {analysis.title} @ {analysis.company} — fit {analysis.fit_score}/100 ({analysis.recommendation})")

    out = job_dir(analysis)
    out.mkdir(parents=True, exist_ok=True)
    (out / "jd.txt").write_text(jd_text)
    (out / "analysis.json").write_text(analysis.model_dump_json(indent=2))

    if rewrite:
        log("Tailoring the resume (Claude)…")
        resume = tailor(llm, profile, analysis, jd_text)
        log("Fact checking…")
        issues = _check(llm, profile, resume)
    else:
        # Free / local engines: your own lines, ranked for this job. Rule checks still run.
        log("Building the resume from your own lines, ranked for this job…")
        resume = heuristics.tailor(profile, analysis)
        issues = rule_issues(profile, resume)
    history = [{"round": 1, "issues": [i.model_dump() for i in issues]}]

    if issues and rewrite:
        log(f"  {len(issues)} line(s) rejected; revising…")
        resume = revise(llm, profile, analysis, jd_text, resume, [f"{i.where}: {i.text} — {i.problem}" for i in issues])
        issues = _check(llm, profile, resume)
        history.append({"round": 2, "issues": [i.model_dump() for i in issues]})

    if issues:
        log(f"  {len(issues)} line(s) still unverified; replacing them with source facts verbatim")
        resume = fall_back_to_sources(profile, resume, issues)
        remaining = rule_issues(profile, resume)
        history.append({"round": "fallback", "issues": [i.model_dump() for i in remaining]})
    else:
        remaining = []

    stem = (config.settings().get("resume") or {}).get("file_stem") or "_".join(profile.contact.name.split()[:2]) or "Resume"
    name = slug(analysis.title) if analysis.company == "unknown" else f"{slug(analysis.company)}_{slug(analysis.title)}"
    pdf_path = out / f"{stem}_{name}.pdf"
    max_pages = config.settings()["resume"]["max_pages"]
    resume, html, pages = _render_within(profile, resume, pdf_path, max_pages)

    (out / "tailored.json").write_text(json.dumps(resume.model_dump(), indent=2))
    (out / "verification.json").write_text(json.dumps(history, indent=2))
    (out / "resume.html").write_text(html)

    before, _ = keyword_coverage(analysis.keywords, profile_text(profile))
    after, missing = keyword_coverage(analysis.keywords, plain_text(html))

    report = {
        "job": {"title": analysis.title, "company": analysis.company, "location": analysis.location},
        "fit_score": analysis.fit_score,
        "recommendation": analysis.recommendation,
        "fit_reason": analysis.fit_reason,
        "gaps": analysis.gaps,
        # The master profile holds every true fact, so its coverage is the
        # ceiling; a good tailored resume keeps all of it in two pages.
        "keyword_coverage": {
            "tailored_resume": f"{len(after)}/{len(analysis.keywords)}",
            "supported_by_profile": f"{len(before)}/{len(analysis.keywords)}",
            "still_missing": missing,
        },
        "verification": "passed" if not remaining else f"{len(remaining)} rule issue(s) left",
        "revision_rounds": len(history),
        "pages": pages,
        "pdf": pdf_path.name,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))

    log(f"  Keywords covered: {len(after)}/{len(analysis.keywords)} (profile supports {len(before)})")
    log(f"  Verification: {report['verification']} · {pages} page(s)")
    if pages > max_pages:
        log(f"  Note: resume still runs to {pages} pages after trimming")
    log(f"Done: {pdf_path}")
    return Built(out, pdf_path, report)
