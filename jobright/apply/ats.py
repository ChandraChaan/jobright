"""Company application forms on Greenhouse, Lever and Ashby."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from jobright.apply import Outcome, guard
from jobright.apply.forms import field_errors, fill, screenshot
from jobright.apply.values import JobContext

_SUBMIT = re.compile(r"^\s*(submit( application)?|apply|send application)\s*$", re.I)
_DONE = re.compile(r"thank you for (applying|your (application|interest))|application (has been |was )?(submitted|received)|"
                   r"we('ve| have) received your application|successfully submitted|thanks for applying", re.I)


def ats_kind(url: str) -> str | None:
    host = re.sub(r"^https?://", "", url or "").split("/")[0]
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    return None


def _open_form(page: Page, url: str, kind: str) -> None:
    if kind == "lever" and not url.rstrip("/").endswith("/apply"):
        url = url.rstrip("/") + "/apply"
    if kind == "ashby" and "/application" not in url:
        url = url.rstrip("/") + "/application"
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if kind == "greenhouse":
        # New job-boards UI shows the form after an Apply button; old boards inline it.
        btn = page.get_by_role("button", name=re.compile(r"^apply", re.I))
        if btn.count() and not page.locator("input[type=file]").count():
            btn.first.click()
            page.wait_for_timeout(1500)


def apply(page: Page, job: dict, resume_pdf: Path, dry_run: bool = False,
          ctx: JobContext | None = None, log=lambda m: None) -> Outcome:
    url = job.get("apply_url") or job["url"]
    kind = ats_kind(url) or job.get("apply_type")
    ctx = ctx or JobContext(job_id=job["id"], source=kind)
    notes: list[str] = []
    _open_form(page, url, kind)
    issue = guard.page_issue(page)
    if issue:
        return Outcome("unavailable" if issue.kind == "unavailable" else "blocked", issue.detail,
                       screenshot=screenshot(page, job["id"], issue.kind))
    notes += guard.clear(page, log=log)
    if _DONE.search(page.inner_text("body")[:5000]):
        return Outcome("applied", "already applied", evidence=_DONE.search(page.inner_text("body")[:5000]).group(0))

    form = page.locator("form").filter(has=page.locator("input[type=file], input[type=email]")).first
    root = form if form.count() else None
    audit: list[dict] = []
    for attempt in range(3):
        result = fill(page, root, resume_pdf, ctx)
        audit += result.audit
        notes += [f"corrected pre-filled answer: {c}" for c in result.corrected]
        if result.missing:
            reason = next((f for f in result.flags if f.startswith("salary review")),
                          "required questions only you can answer")
            return Outcome("needs_you", reason, result.missing, screenshot(page, job["id"], "missing"),
                           audit=audit, notes=notes)
        if dry_run:
            return Outcome("dry_run", f"filled {len(result.filled)} fields; stopped before submit",
                           screenshot=screenshot(page, job["id"], "filled"), audit=audit, notes=notes)
        submit = page.get_by_role("button", name=_SUBMIT)
        if not submit.count():
            submit = page.locator("button[type=submit], input[type=submit]")
        if not submit.count():
            return Outcome("needs_you", "no submit button found", screenshot=screenshot(page, job["id"], "no_submit"))
        submit.last.click()
        for _ in range(20):
            page.wait_for_timeout(1000)
            issue = guard.page_issue(page)
            if issue and issue.kind == "security":
                return Outcome("needs_you", "the form asked for a CAPTCHA", screenshot=screenshot(page, job["id"], "captcha"),
                               audit=audit, notes=notes)
            m = _DONE.search(page.inner_text("body")[:6000])
            if m:
                return Outcome("applied", f"{kind} confirmation shown", evidence=m.group(0),
                               screenshot=screenshot(page, job["id"], "done"), audit=audit, notes=notes)
            if field_errors(root or page.locator("body")):
                break
        errors = field_errors(root or page.locator("body"))
        if not errors:
            return Outcome("unverified", "submitted; no confirmation text seen", audit=audit, notes=notes,
                           screenshot=screenshot(page, job["id"], "after_submit"))
        notes.append(f"form errors after submit (attempt {attempt + 1}): {'; '.join(errors[:3])}")
    return Outcome("needs_you", "the form kept rejecting answers: " + "; ".join(errors[:3]), errors,
                   screenshot(page, job["id"], "errors"), audit=audit, notes=notes)
