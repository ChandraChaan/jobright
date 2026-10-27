"""One scheduled run: discover → filter → analyze → tailor → apply → report."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime

from jobright import config, store
from jobright.jd import JobAnalysis, analyze, skill_match, years_band
from jobright.llm import LLM, LLMError
from jobright.pipeline import build_resume, profile_text
from jobright.profile import load_profile
from jobright.sources import Posting

LOCK_PATH = config.DATA / "run.lock"


@contextmanager
def run_lock():
    """One run at a time: a run and a dashboard 'apply now' never share the browser."""
    from jobright.lock import Busy, hold
    try:
        with hold(LOCK_PATH):
            yield
    except Busy:
        raise SystemExit("Another jobright run is in progress.")


def _over_budget() -> bool:
    return store.spend(date.today().strftime("%Y-%m")) >= config.settings()["limits"]["monthly_budget_usd"]


# --- discovery ---------------------------------------------------------------

def _store(p: Posting) -> bool:
    return store.add_job(source=p.source, external_id=p.external_id, url=p.url, title=p.title,
                         company=p.company, location=p.location, description=p.description,
                         apply_url=p.apply_url, apply_type=p.apply_type, posted_at=p.posted_at)


def _known(source: str):
    return lambda external_id: store.get_job(f"{source}:{external_id}") is not None


def discover(sources: set[str], log=print) -> int:
    s = config.settings()
    new = 0
    if sources & {"greenhouse", "lever", "ashby", "ats"}:
        from jobright.sources import ats
        boards = {k: v for k, v in (s.get("ats_boards") or {}).items()
                  if "ats" in sources or k in sources}
        count = sum(_store(p) for p in ats.discover(boards, log) if _prefilter_ok(p))
        log(f"  company boards: {count} new relevant jobs")
        new += count
    title_ok = lambda t: filter_reason(t, "", "") is None
    if "reed" in sources and s["searches"].get("reed"):
        from jobright.sources import reed
        for p in reed.discover(s["searches"]["reed"], _known("reed"), title_ok=title_ok, log=log):
            new += _store(p)
    browser_sources = sources & {"linkedin", "naukri", "indeed"}
    if browser_sources:
        from jobright.browser import open_browser
        from jobright.sources import indeed, linkedin, naukri
        with open_browser() as ctx:
            if "linkedin" in browser_sources and s["searches"].get("linkedin"):
                for p in linkedin.discover(ctx, s["searches"]["linkedin"], _known("linkedin"),
                                           max_pages=s["limits"].get("linkedin_pages_per_run", 40),
                                           title_ok=title_ok, log=log):
                    new += _store(p)
            if "naukri" in browser_sources and s["searches"].get("naukri"):
                for p in naukri.discover(ctx, s["searches"]["naukri"], _known("naukri"),
                                         title_ok=title_ok, log=log):
                    new += _store(p)
            if "indeed" in browser_sources and s["searches"].get("indeed"):
                for p in indeed.discover(ctx, s["searches"]["indeed"], _known("indeed"),
                                         title_ok=title_ok, log=log):
                    new += _store(p)
    return new


# --- free filters ------------------------------------------------------------

def _words(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9+#]+", " ", s.lower()) + " "


def track_of(title: str) -> str | None:
    """The role track a title belongs to: the first track (in settings order)
    with a matching title phrase, so "Lead Flutter Developer" is mobile."""
    tw = _words(title)
    for name, track in (config.settings().get("tracks") or {}).items():
        if any(_words(x) in tw for x in track.get("titles", [])):
            return name
    return None


def filter_reason(title: str, company: str, location: str, source: str | None = None) -> str | None:
    t = config.settings()["targets"]
    tw = _words(title)
    if any(_words(x).strip() and _words(x) in tw for x in t["title_exclude"]):
        return "title excluded"
    if track_of(title) is None:
        return "title does not match any role track"
    cw = _words(company)
    if any(_words(x) in cw for x in config.settings().get("exclude_companies") or []):
        return "company excluded"
    national = config.country().get("national_sites") or []
    if source in national and location and config.country().get("name", "").lower() not in location.lower():
        location += ", " + config.country().get("name", "")   # national boards list only local jobs
    if location:
        lw = _words(location)
        places = [x for x in t["locations"] if x.lower() != "remote"]
        if any(_words(x) in lw for x in places):
            return None
        # "Remote" counts only when the location names no other place ("Remote", "Remote / Hybrid"),
        # not "Remote - California".
        remote_only = re.sub(r"\b(remote|hybrid|work from home|wfh|anywhere|on site|onsite)\b", " ", lw).strip(" -/,()")
        # Search sites are already limited to your country; company boards list jobs worldwide.
        country_searched = source in ("linkedin", "indeed", "reed", "naukri")
        if any(x.lower() == "remote" for x in t["locations"]) and " remote " in lw and not remote_only.strip() \
                and country_searched:
            return None
        return f"location {location!r} not in target locations"
    return None


def _prefilter_ok(p: Posting) -> bool:
    """Company boards list every job at the company; store only plausible ones."""
    return filter_reason(p.title, p.company, p.location) is None


def apply_filters(log=print) -> None:
    for job in store.jobs("discovered", limit=5000):
        reason = filter_reason(job["title"], job["company"], job["location"] or "", job["source"])
        if reason:
            store.update_job(job["id"], status="filtered", reason=reason)
        else:
            store.update_job(job["id"], track=track_of(job["title"]))


def refilter(log=print) -> int:
    """Give jobs filtered under older rules another look under the current tracks."""
    n = 0
    for job in store.jobs("filtered", limit=5000):
        if filter_reason(job["title"], job["company"], job["location"] or "", job["source"]) is None:
            store.update_job(job["id"], status="discovered", reason=None, track=track_of(job["title"]))
            n += 1
    if n:
        log(f"  {n} previously filtered job(s) match the current role tracks; queued for screening")
    return n


# --- analysis ----------------------------------------------------------------

def _answer_bank() -> dict:
    from jobright.answers import bank
    return bank()


def _track(job: dict) -> tuple[str, dict]:
    name = job.get("track") or track_of(job["title"]) or "mobile"
    return name, (config.settings().get("tracks") or {}).get(name, {})


def priority_score(track: dict, fit: int, skill: int, growth: int, budget_max: float | None,
                   expected: float | None, current: float | None = None) -> int:
    """0-100 rank among jobs that pass: fit and skills say you can win it, growth and
    the job's own budget say whether it is worth winning. Weighted by the track.

    Pay: 100 at or above your expected CTC, 60-100 between current and expected,
    under 20 below your current CTC, 60 when the budget is not stated."""
    current = current or (expected * 0.8 if expected else None)
    if budget_max and expected:
        if budget_max >= expected:
            pay = 100.0
        elif budget_max >= current:
            pay = 60.0 + 40.0 * (budget_max - current) / max(expected - current, 1e-9)
        else:
            pay = 20.0 * budget_max / current
    else:
        pay = 60.0
    raw = 0.40 * fit + 0.25 * skill + 0.20 * growth + 0.15 * pay
    return max(0, min(100, round(raw * float(track.get("weight", 1.0)))))


def judge(job: dict, analysis: JobAnalysis, ptext: str) -> tuple[str, str, dict]:
    """Decide a job from its analysis: (status, reason, fields to store).

    Rules, in order: skill match and the track's fit bar, the model's realism
    call for stretch tracks, experience band (skip when the JD needs clearly
    more years than you have; note when it asks for far fewer), then budget:
    below your current CTC goes to salary_review for you to decide.
    """
    from jobright import salary

    rules = config.settings()["apply_rules"]
    b = _answer_bank()
    track_name, track = _track(job)
    min_fit = track.get("min_fit", rules["min_fit_score"])
    pct, have, missing = skill_match(analysis, ptext)
    notes = [f"{track_name} track", f"skill match {pct}% (has {', '.join(have) or '-'}; missing {', '.join(missing) or '-'})",
             f"fit {analysis.fit_score} (bar {min_fit})", f"growth {analysis.growth_score}"]
    fields: dict = {"fit_score": analysis.fit_score, "skill_match": pct, "analysis_json": analysis.model_dump_json(),
                    "track": track_name}

    status = "screened" if pct >= rules["min_skill_match"] and analysis.fit_score >= min_fit else "skipped"
    if status == "screened" and not analysis.realistic:
        status = "skipped"
        notes.append(f"not realistic yet: {analysis.realism_reason}")

    lo, hi = years_band(analysis.years_required, job["description"] or "")
    fields["years_required"] = analysis.years_required
    raw_total = b.get("total_experience_years")
    total = float(raw_total) if isinstance(raw_total, (int, float)) else None
    if total is not None and lo is not None:
        if lo > total + rules.get("max_years_short", 1):
            status = "skipped"
            notes.append(f"asks for {lo:g}+ years; you have {total:g}")
        elif hi is not None and hi < total - rules.get("overqualified_gap", 4):
            notes.append(f"asks for {lo:g}-{hi:g} years; you have {total:g} (may read as overqualified)")
            if rules.get("skip_overqualified"):
                status = "skipped"

    from jobright.apply.values import salary_facts

    unit, cur, exp = salary_facts(b)
    currency = config.country().get("currency", "INR")
    style = "lpa" if unit == "lpa" else "annual"
    budget = salary.parse_budget(job["description"] or "", currency, style) or \
        salary.parse_budget(analysis.salary_text, currency, style)
    if budget:
        fields["salary_text"] = budget.text
        fields["budget_max_lpa"] = budget.max        # in your salary unit (LPA in India)
        if status == "screened" and cur is not None and exp:
            d = salary.expected_ctc(budget.max, cur, exp, style=style)
            notes.append(d.reason)
            if d.expected is None:
                status = "salary_review"
    fields["priority"] = priority_score(track, analysis.fit_score, pct, analysis.growth_score,
                                        budget.max if budget else None, exp, cur)
    notes.append(f"priority {fields['priority']}")
    return status, "; ".join(notes), fields


def _jd(job: dict) -> str:
    return f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n\n{job['description']}"


def screen(llm: LLM, log=print) -> None:
    s = config.settings()
    profile = load_profile()
    ptext = profile_text(profile)
    deep_left = s["limits"].get("deep_checks_per_run", 15)
    todo = store.jobs("discovered", limit=s["limits"]["analyze_per_run"], order="discovered_at DESC")
    if todo:
        log(f"Analyzing {len(todo)} job description(s)…")
    for job in todo:
        if _over_budget():
            log("  monthly budget reached; stopping paid steps")
            return
        dup = store.find_duplicate(job["company"], job["title"], exclude_id=job["id"])
        if dup and dup["status"] != "discovered":
            store.update_job(job["id"], status="duplicate", reason=f"same job as {dup['id']} ({dup['status']})")
            continue
        try:
            analysis = analyze(llm, profile, _jd(job))
        except LLMError as e:
            store.update_job(job["id"], error=str(e))
            log(f"  {job['title']} @ {job['company']}: {e}")
            continue
        status, reason, fields = judge(job, analysis, ptext)
        track_name, track = _track(job)
        # A stretch role that passes the quick read gets a second, deeper opinion
        # before any money is spent on a tailored resume.
        if status == "screened" and track.get("stretch") and deep_left > 0:
            deep_left -= 1
            try:
                deep = analyze(llm, profile, _jd(job), deep=True)
                status, reason, fields = judge(job, deep, ptext)
                reason = "deep check: " + reason
                fields["deep_checked"] = 1
            except LLMError as e:
                status, reason = "discovered", f"deep check failed, will retry: {e}"
        elif status == "screened" and track.get("stretch"):
            status, reason = "discovered", "waiting for a deep check (per-run limit reached)"
        store.update_job(job["id"], status=status, reason=reason, **fields)
        mark = {"screened": "✓", "salary_review": "$"}.get(status, "·")
        log(f"  {mark} {track_name:15} {fields['skill_match']:>3}% skills · fit {fields['fit_score']:>3} · "
            f"prio {fields.get('priority', 0):>3} · {job['title']} @ {job['company']}")


def tailor_screened(llm: LLM, log=print) -> None:
    s = config.settings()
    budget = s["limits"]["tailored_per_day"] - store.tailored_today()
    todo = store.jobs("screened", limit=max(budget, 0), order="priority DESC, fit_score DESC")
    profile = load_profile()
    for job in todo:
        if _over_budget():
            log("  monthly budget reached; stopping paid steps")
            return
        dup = store.find_duplicate(job["company"], job["title"], exclude_id=job["id"],
                                   statuses=("ready", "approved", "unverified", "applied", "interview", "offer"))
        if dup:
            store.update_job(job["id"], status="duplicate", reason=f"same job as {dup['id']} ({dup['status']})")
            continue
        analysis = JobAnalysis.model_validate_json(job["analysis_json"])
        jd = _jd(job)
        log(f"Tailoring for {job['title']} @ {job['company']}…")
        try:
            built = build_resume(profile, jd, llm=llm, analysis=analysis, log=lambda m: log("  " + m))
        except LLMError as e:
            store.update_job(job["id"], error=str(e))
            continue
        store.log_event(job["id"], "tailored", built.report["verification"])
        auto = s["apply"]["auto_submit"].get(_auto_key(job), False)
        store.update_job(job["id"], status="approved" if auto else "ready",
                         out_dir=str(built.out_dir), resume_pdf=str(built.pdf))


def _auto_key(job: dict) -> str:
    return job["source"] if job["source"] in ("linkedin", "naukri", "reed", "indeed") else "ats"


# --- applying ----------------------------------------------------------------

def requeue_answered(log=print) -> int:
    """Jobs waiting only on questions you have since answered go back to approved."""
    from jobright.answers import answer_field, norm_q, pending_questions
    from jobright.apply.dispatch import job_context
    from jobright.apply.fields import FieldSpec

    open_q = {norm_q(p["question"]) for p in pending_questions()}
    n = 0
    for job in store.jobs("needs_you", limit=500):
        if not job.get("missing_json") or not job.get("resume_pdf"):
            continue
        missing = [re.sub(r"\s*\((site says|site pre-filled|could not fill|Naukri did not).*$", "", m)
                   for m in json.loads(job["missing_json"])]
        if any(norm_q(m) in open_q for m in missing):
            continue
        ctx = job_context(job)
        if all(answer_field(FieldSpec(n="-", kind="text", label=m), ctx, record_missing=False) for m in missing):
            store.update_job(job["id"], status="approved", reason="your answers now cover every question; retrying")
            n += 1
    if n:
        log(f"Re-queued {n} job(s) whose questions you have answered")
    return n


def apply_approved(dry_run: bool = False, job_ids: list[str] | None = None, log=print) -> None:
    from jobright.apply.dispatch import apply_jobs, limit_key, verify_jobs

    s = config.settings()["limits"]
    if not job_ids and not dry_run:
        unverified = store.jobs("unverified", limit=50)
        if unverified:
            log(f"Re-checking {len(unverified)} submission(s) that were not confirmed…")
            verify_jobs(unverified, log=log)
        requeue_answered(log)
    jobs = [store.get_job(j) for j in job_ids] if job_ids else store.jobs("approved", order="priority DESC, updated_at ASC")
    jobs = [j for j in jobs if j]
    picked, used = [], {}
    for job in jobs:
        src = job["source"] if job["source"] in ("linkedin", "naukri", "reed", "indeed") else "ats"
        if src not in used:
            used[src] = store.applied_today(job["source"]) if src != "ats" else sum(
                store.applied_today(x) for x in ("greenhouse", "lever", "ashby"))
        if used[src] >= s[limit_key(job)]:
            continue
        used[src] += 1
        picked.append(job)
    if picked:
        log(f"Applying to {len(picked)} job(s){' (dry run)' if dry_run else ''}…")
    apply_jobs(picked, dry_run=dry_run, log=log)


# --- the whole run -----------------------------------------------------------

def run(sources: set[str] | None = None, apply: bool = True, log=print) -> dict:
    sources = sources or set((config.settings().get("profile") or {}).get("sites") or ["ats", "linkedin", "naukri"])
    started = datetime.now()
    with run_lock():
        log(f"[{started:%Y-%m-%d %H:%M}] jobright run: {', '.join(sorted(sources))}")
        new = discover(sources, log)
        log(f"Discovered {new} new job(s)")
        refilter(log)
        apply_filters(log)
        llm = LLM()
        screen(llm, log)
        tailor_screened(llm, log)
        if apply:
            apply_approved(log=log)
        from jobright.report import write_daily
        path = write_daily()
        summary = {"new": new, "counts": store.status_counts(),
                   "spend_today": round(store.spend(date.today().isoformat()), 4), "report": str(path)}
        store.log_event(None, "run", summary)
        log(json.dumps(summary))
        return summary
