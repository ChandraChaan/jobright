"""A multi-step application form, the way most job sites build them.

Used for Reed and Indeed (and any site added later): open the job, press its
apply button, then repeat — clear popups, fill this step, press the primary
button (Continue / Next / Review / Submit) — until the site confirms. Stuck
steps, validation errors, missing answers, bot checks and removed postings
end the loop with the same Outcomes as the LinkedIn engine; Submit without a
confirmation is `unverified`, never `failed`.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Locator, Page

from jobright.apply import Outcome, guard
from jobright.apply.forms import field_errors, fill, screenshot
from jobright.apply.values import JobContext


@dataclass
class Site:
    name: str
    start: re.Pattern               # the job page's apply button
    submit: re.Pattern              # the final submit button
    advance: re.Pattern             # continue / next / review
    done: re.Pattern                # confirmation text
    applied: re.Pattern             # "you already applied" on the job page


def _button(scope: Page | Locator, rx: re.Pattern) -> Locator | None:
    for role in ("button", "link"):
        b = scope.get_by_role(role, name=rx)
        for i in range(min(b.count(), 5)):
            if b.nth(i).is_visible() and b.nth(i).is_enabled():
                return b.nth(i)
    return None


def _signature(page: Page) -> str:
    try:
        t = page.inner_text("main", timeout=3000) if page.locator("main").count() else page.inner_text("body", timeout=3000)
    except Exception:
        t = ""
    return hashlib.md5((page.url.split("?")[0] + t[:1500]).encode()).hexdigest()


def run(page: Page, job: dict, resume_pdf: Path, site: Site, dry_run: bool = False,
        ctx: JobContext | None = None, log=lambda m: None) -> Outcome:
    ctx = ctx or JobContext(job_id=job["id"], source=site.name)
    notes: list[str] = []
    audit: list[dict] = []
    page.goto(job["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    issue = guard.page_issue(page)
    if issue:
        return Outcome("unavailable" if issue.kind == "unavailable" else "blocked", issue.detail,
                       screenshot=screenshot(page, job["id"], issue.kind))
    if site.applied.search(page.inner_text("body")[:20000]):
        return Outcome("applied", f"already applied on {site.name}", evidence=site.applied.search(page.inner_text("body")[:20000]).group(0))
    notes += guard.clear(page, log=log)
    start = _button(page, site.start)
    if not start:
        return Outcome("needs_you", f"no apply button on {site.name}", screenshot=screenshot(page, job["id"], "no_apply"))
    try:
        with page.context.expect_page(timeout=4000) as new:
            start.click()
        page = new.value                     # some sites open the form in a new tab
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(2500)

    seen: Counter[str] = Counter()
    uploaded = False
    submitted = False
    errors: list[str] = []
    for turn in range(25):
        issue = guard.page_issue(page)
        if issue and issue.kind != "unavailable":
            return Outcome("blocked", issue.detail, screenshot=screenshot(page, job["id"], "blocked"), notes=notes)
        body = page.inner_text("body")[:20000]
        m = site.done.search(body)
        if m:
            return Outcome("applied", f"{site.name} confirmed", evidence=m.group(0), audit=audit, notes=notes,
                           screenshot=screenshot(page, job["id"], "done"))
        if submitted:
            return Outcome("unverified", f"submitted on {site.name}; no confirmation seen yet", audit=audit, notes=notes,
                           screenshot=screenshot(page, job["id"], "unverified"))
        notes += guard.clear(page, log=log)
        sig = _signature(page)
        seen[sig] += 1
        if seen[sig] > 3:
            return Outcome("needs_you", f"stuck on a {site.name} step" + (f": {'; '.join(errors[:3])}" if errors else ""),
                           errors, screenshot(page, job["id"], "stuck"), audit=audit, notes=notes)
        if not uploaded and page.locator("input[type=file]").count():
            try:
                page.locator("input[type=file]").first.set_input_files(str(resume_pdf))
                page.wait_for_timeout(2000)
                uploaded = True
            except Exception:
                pass
        result = fill(page, None, resume_pdf, ctx)
        audit += result.audit
        notes += [f"corrected pre-filled answer: {c}" for c in result.corrected]
        if result.missing:
            reason = next((f for f in result.flags if f.startswith("salary review")),
                          f"{site.name} asked questions only you can answer")
            return Outcome("needs_you", reason, result.missing, screenshot(page, job["id"], f"step{turn}_missing"),
                           audit=audit, notes=notes)
        submit = _button(page, site.submit)
        if submit:
            if dry_run:
                return Outcome("dry_run", f"reached Submit after {turn + 1} step(s)",
                               screenshot=screenshot(page, job["id"], "review"), audit=audit, notes=notes)
            submit.click()
            submitted = True
            page.wait_for_timeout(4000)
            continue
        nxt = _button(page, site.advance)
        if not nxt:
            return Outcome("needs_you", f"no Continue / Submit button on a {site.name} step",
                           screenshot=screenshot(page, job["id"], f"step{turn}_no_button"), audit=audit, notes=notes)
        nxt.click()
        page.wait_for_timeout(2000)
        errors = field_errors(page.locator("body"))
    return Outcome("needs_you", f"{site.name} application did not finish in 25 steps", notes=notes)


REED = Site(
    name="reed",
    start=re.compile(r"^(apply now|apply|easy apply)$", re.I),
    submit=re.compile(r"^(submit application|send application|apply now|submit)$", re.I),
    advance=re.compile(r"^(continue|next|review)$", re.I),
    done=re.compile(r"application (has been )?sent|you('ve| have) applied|thanks for applying|application submitted", re.I),
    applied=re.compile(r"you applied (for this job )?on|you('ve| have) already applied", re.I),
)

INDEED = Site(
    name="indeed",
    start=re.compile(r"^(apply now|easily apply|apply)$", re.I),
    submit=re.compile(r"^submit (your )?application$", re.I),
    advance=re.compile(r"^(continue|next|review( your application)?)$", re.I),
    done=re.compile(r"your application has been submitted|application submitted|you('ve| have) applied", re.I),
    applied=re.compile(r"you applied|applied on \d", re.I),
)
