"""LinkedIn Easy Apply as a state machine.

Each turn of the loop looks at the page as it is now, not as the last click
assumed it would be:

  posting gone / security check     → unavailable / blocked
  a popup on top                    → close it safely, look again
  the Easy Apply dialog             → fill this step, press its primary
                                      button, check that the step changed;
                                      on a validation error, re-answer from
                                      the error text; on no progress three
                                      times, hand over
  dialog gone, confirmation shown   → applied
  dialog gone, nothing shown        → an interruption closed it: reopen
                                      Easy Apply (LinkedIn restores the
                                      draft at the same step) up to twice

Submission is only reported as applied with evidence: LinkedIn's "Your
application was sent" dialog, or "Application submitted" on a fresh load of
the job page. Pressing Submit without either gives `unverified`, which the
next run re-checks instead of submitting again.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from playwright.sync_api import Locator, Page

from jobright.apply import Outcome
from jobright.apply import guard
from jobright.apply.forms import field_errors, fill, screenshot
from jobright.apply.values import JobContext

_SUBMITTED = re.compile(r"application submitted|your application was sent|application was sent to|"
                        r"applied \d+ (second|minute|hour|day|week|month)s? ago", re.I)
_PRIMARY = [("submit", re.compile(r"^submit application$", re.I)),
            ("review", re.compile(r"^review( your application)?$", re.I)),
            ("next", re.compile(r"^(next|continue( to next step)?)$", re.I))]


def submitted(page: Page) -> str | None:
    """The confirmation text LinkedIn shows for this job, if any."""
    try:
        m = _SUBMITTED.search(page.inner_text("body", timeout=5000)[:40000])
    except Exception:
        return None
    return m.group(0) if m else None


def _easy_dialog(page: Page) -> Locator | None:
    d = page.locator("dialog[open], div[role=dialog], [aria-modal=true]").filter(
        has_text=re.compile(r"^\s*Apply to |Contact info|Resume|Additional Questions|Review your application|Submit application", re.I | re.M))
    d = d.filter(has=page.get_by_role("button", name=re.compile(r"^(dismiss|next|review|submit application|continue|back)", re.I)))
    return d.last if d.count() else None


def _confirmation(page: Page) -> str | None:
    d = page.locator("dialog[open], div[role=dialog], [aria-modal=true]").filter(
        has_text=re.compile(r"application was sent|application submitted", re.I))
    if d.count():
        m = _SUBMITTED.search(d.last.inner_text())
        return m.group(0) if m else "Your application was sent"
    return None


def _close_confirmation(page: Page) -> None:
    d = page.locator("dialog[open], div[role=dialog], [aria-modal=true]").filter(
        has_text=re.compile(r"application was sent|application submitted", re.I))
    if d.count():
        for name in (r"^not now$", r"^done$", r"^dismiss$"):
            b = d.last.get_by_role("button", name=re.compile(name, re.I))
            if b.count():
                b.first.click()
                return


def _primary(dialog: Locator) -> tuple[str, Locator] | None:
    for kind, rx in _PRIMARY:
        b = dialog.get_by_role("button", name=rx)
        if b.count() and b.first.is_enabled():
            return kind, b.first
    return None


def _step_signature(dialog: Locator) -> str:
    try:
        t = dialog.inner_text(timeout=3000)
    except Exception:
        return ""
    progress = re.search(r"\d+\s*/\s*\d+ pages?|\d+%", t)
    head = "\n".join(l for l in t.splitlines()[:12] if l.strip())
    return hashlib.md5(((progress.group(0) if progress else "") + head).encode()).hexdigest()


def _discard(page: Page) -> None:
    d = _easy_dialog(page)
    if not d:
        return
    try:
        x = d.get_by_role("button", name=re.compile(r"^dismiss$", re.I))
        if x.count():
            x.first.click()
            page.wait_for_timeout(800)
        discard = page.get_by_role("button", name=re.compile(r"^discard$", re.I))
        if discard.count():
            discard.first.click()
    except Exception:
        pass


def _open(page: Page) -> bool:
    for loc in (page.get_by_role("button", name=re.compile(r"easy apply", re.I)),
                page.get_by_role("link", name=re.compile(r"^easy apply", re.I))):
        if loc.count():
            loc.first.click()
            page.wait_for_timeout(2200)
            return True
    return False


def external_url(page: Page, job: dict) -> str | None:
    """Follow LinkedIn's 'Apply' (company website) button to the real form URL."""
    page.goto(job["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if guard.page_issue(page):
        return None
    guard.clear(page)
    btn = page.get_by_role("button", name=re.compile(r"^apply\b", re.I))
    if not btn.count():
        btn = page.get_by_role("link", name=re.compile(r"^apply\b", re.I))
    if not btn.count():
        return None
    try:
        with page.context.expect_page(timeout=15000) as new:
            btn.first.click()
        tab = new.value
        tab.wait_for_load_state("domcontentloaded")
        url = tab.url
        tab.close()
        return None if "linkedin.com" in url else url
    except Exception:
        return None


def apply(page: Page, job: dict, resume_pdf: Path, dry_run: bool = False,
          ctx: JobContext | None = None, log=lambda m: None) -> Outcome:
    ctx = ctx or JobContext(job_id=job["id"], source="linkedin")
    notes: list[str] = []
    audit: list[dict] = []

    page.goto(job["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    issue = guard.page_issue(page)
    if issue:
        return Outcome("unavailable" if issue.kind == "unavailable" else "blocked", issue.detail,
                       screenshot=screenshot(page, job["id"], issue.kind))
    proof = submitted(page)
    if proof:
        return Outcome("applied", "already applied on LinkedIn", evidence=proof)
    notes += guard.clear(page, log=log)
    if not _open(page):
        issue = guard.page_issue(page)
        if issue:
            return Outcome("unavailable", issue.detail)
        return Outcome("needs_you", "no Easy Apply button (external apply or closed)",
                       screenshot=screenshot(page, job["id"], "no_easy_apply"))

    seen: Counter[str] = Counter()
    reopened = 0
    uploaded = False
    submit_pressed = False
    last_errors: list[str] = []
    for turn in range(30):
        page.wait_for_timeout(900)
        issue = guard.page_issue(page)
        if issue and issue.kind != "unavailable":
            return Outcome("blocked", issue.detail, screenshot=screenshot(page, job["id"], "blocked"), notes=notes)

        proof = _confirmation(page)
        if proof:
            _close_confirmation(page)
            return Outcome("applied", "Easy Apply submitted", evidence=proof, audit=audit, notes=notes)

        dialog = _easy_dialog(page)
        if dialog is None:
            closed = guard.clear(page, log=log)
            notes += closed
            if closed:
                continue
            proof = submitted(page)
            if proof:
                return Outcome("applied", "Easy Apply submitted", evidence=proof, audit=audit, notes=notes)
            if submit_pressed:
                page.goto(job["url"], wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                proof = submitted(page)
                if proof:
                    return Outcome("applied", "Easy Apply submitted", evidence=proof, audit=audit, notes=notes)
                return Outcome("unverified", "Submit pressed; LinkedIn showed no confirmation yet",
                               screenshot=screenshot(page, job["id"], "unverified"), audit=audit, notes=notes)
            if reopened < 2 and _open(page):
                reopened += 1
                notes.append("the Easy Apply dialog was closed by an interruption; reopened the saved draft")
                continue
            return Outcome("needs_you", "the Easy Apply dialog kept closing",
                           screenshot=screenshot(page, job["id"], "dialog_gone"), notes=notes)

        notes += guard.clear(page, keep=dialog, log=log)
        sig = _step_signature(dialog)
        seen[sig] += 1
        if seen[sig] > 3:
            shot = screenshot(page, job["id"], "stuck")
            if not dry_run:
                _discard(page)
            return Outcome("needs_you", "stuck on one Easy Apply step" + (f": {'; '.join(last_errors[:3])}" if last_errors else ""),
                           last_errors, shot, audit=audit, notes=notes)

        if not uploaded and dialog.locator("input[type=file]").count():
            dialog.locator("input[type=file]").first.set_input_files(str(resume_pdf))
            page.wait_for_timeout(2500)
            uploaded = True

        result = fill(page, dialog, resume_pdf, ctx)
        audit += result.audit
        notes += [f"corrected pre-filled answer: {c}" for c in result.corrected]
        if result.missing:
            shot = screenshot(page, job["id"], f"step{turn}_missing")
            _discard(page)
            reason = "Easy Apply asked questions only you can answer"
            if any(f.startswith("salary review") for f in result.flags):
                reason = next(f for f in result.flags if f.startswith("salary review"))
            return Outcome("needs_you", reason, result.missing, shot, audit=audit, notes=notes)

        action = _primary(dialog)
        if action is None:
            shot = screenshot(page, job["id"], f"step{turn}_no_button")
            _discard(page)
            return Outcome("needs_you", "no Next / Review / Submit button in the dialog", screenshot=shot, notes=notes)
        kind, button = action
        if kind == "submit":
            shot = screenshot(page, job["id"], "review")
            if dry_run:
                _discard(page)
                return Outcome("dry_run", f"reached Submit after {turn + 1} step(s)", screenshot=shot, audit=audit, notes=notes)
            button.click()
            submit_pressed = True
            page.wait_for_timeout(3500)
            continue
        button.click()
        page.wait_for_timeout(1500)
        dialog = _easy_dialog(page)
        last_errors = field_errors(dialog) if dialog is not None else []
        if last_errors:
            notes.append(f"LinkedIn flagged: {'; '.join(last_errors[:3])}; re-answering from the message")

    _discard(page)
    return Outcome("needs_you", "Easy Apply did not finish in 30 steps", notes=notes)
