"""Route approved jobs to the right apply engine and record what happened.

Before a job is touched: it must not duplicate an application already made
(same company + same or near-identical title), the company must be under its
30-day cap, and the job must be under its attempt limit. A page crash is
retried once on a fresh tab. A security check or sign-out pauses that site
for the rest of the run instead of failing every remaining job.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobright import config, store
from jobright.apply import Outcome, ats, guard, linkedin, naukri, stepper
from jobright.apply.values import JobContext
from jobright.browser import is_logged_in, open_browser, page as first_page

_SOURCE_LIMIT = {"linkedin": "linkedin_per_day", "naukri": "naukri_per_day", "reed": "reed_per_day",
                 "indeed": "indeed_per_day"}
_SITE = {"linkedin": "linkedin", "naukri": "naukri", "reed": "reed", "indeed": "indeed"}
_TRANSIENT = ("TimeoutError", "TargetClosedError", "Error")


def limit_key(job: dict) -> str:
    return _SOURCE_LIMIT.get(job["source"], "ats_per_day")


def job_context(job: dict) -> JobContext:
    return JobContext(job_id=job["id"], source=job["source"], company=job["company"], title=job["title"],
                      budget_max_lpa=job.get("budget_max_lpa"),
                      expected_ctc_override=job.get("expected_ctc_override"))


def _route(pg, job: dict, pdf: Path, dry_run: bool, log) -> Outcome:
    ctx = job_context(job)
    kind = job.get("apply_type")
    if kind == "easy_apply":
        if not is_logged_in(pg.context, "linkedin"):
            return Outcome("blocked", "not signed in to LinkedIn; run `jobright login linkedin`")
        return linkedin.apply(pg, job, pdf, dry_run, ctx, log)
    if kind == "naukri":
        if not is_logged_in(pg.context, "naukri"):
            return Outcome("blocked", "not signed in to Naukri; run `jobright login naukri`")
        return naukri.apply(pg, job, pdf, dry_run, ctx, log)
    if kind in ("greenhouse", "lever", "ashby"):
        return ats.apply(pg, job, pdf, dry_run, ctx, log)
    if kind in ("reed", "indeed"):
        if not is_logged_in(pg.context, kind):
            return Outcome("blocked", f"not signed in to {kind}; run `jobright login {kind}`")
        return stepper.run(pg, job, pdf, stepper.REED if kind == "reed" else stepper.INDEED, dry_run, ctx, log)
    if kind == "external":
        url = job.get("apply_url") if job["source"] not in ("linkedin", "naukri", "indeed") else None
        if job["source"] == "linkedin":
            pg.goto(job["url"], wait_until="domcontentloaded")
            pg.wait_for_timeout(2500)
            issue = guard.page_issue(pg)
            if issue and issue.kind == "unavailable":
                return Outcome("unavailable", issue.detail)
            if linkedin.submitted(pg):
                return Outcome("applied", "already applied", evidence=linkedin.submitted(pg))
            url = linkedin.external_url(pg, job)
        if url and ats.ats_kind(url):
            store.update_job(job["id"], apply_url=url)
            return ats.apply(pg, {**job, "apply_url": url}, pdf, dry_run, ctx, log)
        return Outcome("needs_you", f"company site the bot can't fill: {url or job['url']}")
    return Outcome("needs_you", f"unknown apply type {kind!r}")


def precheck(job: dict) -> Outcome | None:
    """Reasons not to touch a job at all."""
    s = config.settings()
    dup = store.find_duplicate(job["company"], job["title"], exclude_id=job["id"],
                               statuses=("applied", "unverified", "interview", "offer"))
    if dup:
        return Outcome("duplicate", f"already applied to the same job: {dup['id']} ({dup['title']})")
    cap = s["limits"].get("max_per_company_30d", 3)
    if store.applications_to_company(job["company"]) >= cap:
        return Outcome("skipped", f"already {cap} applications to {job['company']} in 30 days")
    if (job.get("attempts") or 0) >= s["limits"].get("max_attempts", 3):
        return Outcome("needs_you", f"gave up after {job['attempts']} attempts; last error: {job.get('error') or job.get('reason')}")
    return None


def _record(job: dict, outcome: Outcome) -> None:
    reason = outcome.detail + (": " + "; ".join(outcome.missing) if outcome.missing else "")
    fields: dict = {"status": outcome.status, "reason": reason[:1500],
                    "error": outcome.detail if outcome.status == "failed" else None}
    if outcome.missing:
        fields["missing_json"] = json.dumps(outcome.missing)
    if outcome.evidence:
        fields["evidence"] = outcome.evidence
    if outcome.audit:
        fields["answers_json"] = json.dumps(outcome.audit)
    if outcome.status == "blocked":       # the job itself is fine; it waits for the next run
        fields = {"reason": reason[:1500]}
    if outcome.status == "needs_you" and outcome.detail.startswith("salary review"):
        fields["status"] = "salary_review"   # the form showed a budget below your current CTC
    store.update_job(job["id"], **fields)


def apply_jobs(jobs: list[dict], dry_run: bool = False, log=print) -> list[tuple[dict, Outcome]]:
    results: list[tuple[dict, Outcome]] = []
    if not jobs:
        return results
    paused: set[str] = set()
    with open_browser() as ctx:
        pg = first_page(ctx)  # popup and alert handling is installed by open_browser
        for job in jobs:
            site = _SITE.get(job["source"], "ats")
            if site in paused:
                continue
            outcome = None if dry_run else precheck(job)
            pdf = Path(job["resume_pdf"]) if job.get("resume_pdf") else None
            if outcome is None and (not pdf or not pdf.exists()):
                outcome = Outcome("failed", "tailored resume PDF is missing")
            if outcome is None:
                if not dry_run:
                    store.bump_attempts(job["id"])
                for attempt in range(2):
                    try:
                        outcome = _route(pg, job, pdf, dry_run, log)
                        break
                    except Exception as e:
                        name = type(e).__name__
                        if attempt == 0 and name in _TRANSIENT:
                            log(f"    {name}; retrying on a fresh tab")
                            try:
                                pg = ctx.new_page()
                            except Exception:
                                pass
                            continue
                        outcome = Outcome("failed", f"{name}: {e}"[:500])
            log(f"  {job['company']} — {job['title']}: {outcome.status} ({outcome.detail})")
            for note in outcome.notes:
                log(f"    · {note}")
            if outcome.status == "blocked":
                paused.add(site)
                log(f"    pausing {site} for this run")
            if not dry_run:
                _record(job, outcome)
            store.log_event(job["id"], "apply" if not dry_run else "dry_run",
                            {"status": outcome.status, "detail": outcome.detail, "missing": outcome.missing,
                             "screenshot": outcome.screenshot, "evidence": outcome.evidence,
                             "notes": outcome.notes, "answers": outcome.audit})
            results.append((job, outcome))
            pg.wait_for_timeout(2500)
    return results


def verify_jobs(jobs: list[dict], log=print) -> None:
    """Re-check `unverified` jobs on the site. Confirmed → applied. Not
    confirmed on LinkedIn/Naukri (which always show it) → approved again, so
    the next attempt starts from a page that says whether it was sent.
    Company forms cannot be re-checked: those stay for you to confirm."""
    if not jobs:
        return
    with open_browser() as ctx:
        pg = first_page(ctx)  # popup and alert handling is installed by open_browser
        for job in jobs:
            proof = None
            try:
                if job["source"] == "linkedin":
                    pg.goto(job["url"], wait_until="domcontentloaded")
                    pg.wait_for_timeout(3500)
                    proof = linkedin.submitted(pg)
                elif job["source"] == "naukri":
                    from jobright.sources.naukri import read_job
                    pg.goto(job["url"], wait_until="domcontentloaded")
                    pg.wait_for_timeout(4000)
                    proof = "Naukri shows Applied" if read_job(pg)["applied"] else None
                else:
                    store.update_job(job["id"], status="needs_you",
                                     reason="submitted on the company site without a confirmation page; "
                                            "check your email, then mark it applied or approve again")
                    continue
            except Exception as e:
                log(f"  verify {job['id']}: {type(e).__name__}")
                continue
            if proof:
                store.update_job(job["id"], status="applied", evidence=proof, reason="confirmed on re-check")
                log(f"  confirmed: {job['company']} — {job['title']} ({proof})")
            else:
                store.update_job(job["id"], status="approved", reason="not confirmed on re-check; will retry")
                log(f"  not confirmed, will retry: {job['company']} — {job['title']}")
