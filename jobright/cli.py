"""Command line: python -m jobright <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jobright import config
from jobright.llm import LLMError
from jobright.profile import load_profile


def cmd_profile(_args) -> None:
    profile = load_profile()
    facts = profile.facts()
    print(f"OK: {profile.contact.name} — {len(profile.roles)} roles, {len(facts)} facts, "
          f"{len(profile.skill_names())} skills")


def cmd_base(_args) -> None:
    from jobright.render import base_view, to_html, write_pdf

    profile = load_profile()
    html = to_html(base_view(profile))
    out = config.DATA / "base"
    out.mkdir(parents=True, exist_ok=True)
    (out / "resume.html").write_text(html)
    pdf = out / f"{_file_stem()}_Master.pdf"
    pages = write_pdf(html, pdf)
    print(f"Master resume: {pdf} ({pages} pages)")


def _file_stem() -> str:
    stem = (config.settings().get("resume") or {}).get("file_stem")
    return stem or "_".join(load_profile().contact.name.split()[:2]) or "Resume"


def cmd_general(_args) -> None:
    """A two-page resume aimed at your core roles, for job-site profiles."""
    import shutil

    from jobright.onboard import load_career
    from jobright.pipeline import build_resume

    plan = load_career()
    if plan is None:
        sys.exit("No career plan yet: finish Setup (jobright start) or run `jobright plan`.")
    core = plan.tracks[0] if plan.tracks else None
    strengths = ", ".join(s.skill for s in plan.strengths if s.depth in ("expert", "solid"))
    brief = (
        f"Target role brief (not a specific job): {core.label if core else plan.primary} roles "
        f"({', '.join(core.titles[:6]) if core else ''}) at {plan.level} level in "
        f"{config.country().get('name', '')}. Hiring teams look for: {strengths}.\n"
        "This resume will be uploaded to job-site profiles, so it should present the candidate's "
        "strongest overall case for these roles."
    )
    built = build_resume(load_profile(), brief)
    out = config.DATA / "base" / f"{_file_stem()}_Resume.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(built.pdf, out)
    print(f"General resume: {out}")


def cmd_import(args) -> None:
    from jobright import onboard

    profile, flagged, _ = onboard.import_resume(Path(args.file))
    path = onboard.save_profile(profile)
    print(f"Profile saved: {path} ({len(profile.roles)} roles, {len(profile.facts())} facts)")
    for fid, text in flagged:
        print(f"  check: [{fid}] {text}")


def cmd_plan(_args) -> None:
    from jobright import onboard

    prof = config.settings().get("profile") or {}
    plan = onboard.plan_career(load_profile(), config.country().get("name", "your country"))
    onboard.save_career(plan)
    print(f"{plan.level} · {plan.years} years · {plan.primary}\n{plan.summary}\n")
    for t in plan.tracks:
        print(f"  {'stretch' if t.stretch else 'core   '} {t.label}: {', '.join(t.search_keywords)}")
    if prof.get("country"):
        onboard.apply_plan(plan, prof["country"], prof.get("cities") or [], prof.get("sites") or ["linkedin", "ats"],
                           config.settings().get("exclude_companies") or [], prof.get("remote_ok", True))
        print("Tracks and searches saved to your settings.")


def cmd_start(args) -> None:
    """Open the app: start the dashboard if it is not running, then open the browser."""
    import subprocess
    import time
    import urllib.request
    import webbrowser

    port = config.settings().get("dashboard", {}).get("port", 8765)
    url = f"http://127.0.0.1:{port}/"
    try:
        urllib.request.urlopen(url, timeout=2)
    except Exception:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log = (config.LOGS_DIR / "dashboard.log").open("a")
        subprocess.Popen([sys.executable, "-m", "jobright", "dashboard"], stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
        for _ in range(30):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(url, timeout=2)
                break
            except Exception:
                pass
    target = url if not args.no_browser else None
    print(f"jobright is running at {url}")
    if target:
        webbrowser.open(target)


def cmd_doctor(_args) -> None:
    """Check the install: Python, browser, API key, profile, settings, schedule."""
    import os

    def line(ok, what, hint=""):
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  → {hint}" if not ok and hint else ""))

    print(f"jobright folder: {config.DATA}")
    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "install Python 3.11 or newer")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        line(True, "browser engine")
    except Exception as e:
        line(False, "browser engine", f"run: {sys.executable} -m playwright install chromium ({type(e).__name__})")
    from jobright import engine
    print(f"  ✓ AI engine: {engine.describe()}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("    optional: a Claude API key adds rewritten, fact-checked resumes (Setup → AI engine)")
    if (config.settings().get("ai") or {}).get("engine") == "local" and not engine.ollama_models():
        print(f"  ✗ Ollama is not running; install it from ollama.com, then `ollama pull {engine.local_model()}`")
    line(config.PROFILE_PATH.exists(), "master profile", "upload your resume in Setup")
    line(bool(config.settings().get("tracks")), "role tracks", "Setup → Career plan")
    line(config.ANSWERS_PATH.exists(), "answers", "Setup → Answers")
    sites = (config.settings().get("profile") or {}).get("sites") or []
    if "reed" in sites:
        line(bool(os.environ.get("REED_API_KEY")), "Reed API key", "free at reed.co.uk/developers; add it in Setup")


def cmd_tailor(args) -> None:
    from jobright.pipeline import build_resume

    jd_text = sys.stdin.read() if args.jd == "-" else Path(args.jd).read_text()
    if len(jd_text.strip()) < 200:
        sys.exit("That job description looks too short; paste the full text.")
    build_resume(load_profile(), jd_text)


def cmd_run(args) -> None:
    from jobright.runner import run

    sources = set(args.sources.split(",")) if args.sources else None
    run(sources, apply=not args.no_apply)


def cmd_discover(args) -> None:
    from jobright.runner import apply_filters, discover, run_lock

    with run_lock():
        sources = args.sources.split(",") if args.sources else \
            ((config.settings().get("profile") or {}).get("sites") or ["ats", "linkedin", "naukri"])
        n = discover(set(sources))
        apply_filters()
        print(f"{n} new job(s)")


def cmd_apply(args) -> None:
    from jobright.runner import apply_approved, run_lock

    with run_lock():
        apply_approved(dry_run=args.dry_run, job_ids=args.job or None)


def cmd_login(args) -> None:
    from jobright.browser import login

    login(args.site)


def cmd_session(_args) -> None:
    from jobright.browser import session_status

    for site, ok in session_status().items():
        print(f"{site:9} {'signed in' if ok else 'not signed in — run: jobright login ' + site}")


def cmd_dashboard(_args) -> None:
    from jobright.dashboard import serve

    serve()


def cmd_schedule(args) -> None:
    from jobright import schedule

    if args.action == "install":
        for p in schedule.install(args.every, dashboard=not args.no_dashboard):
            print(f"Installed {p}")
        print(f"jobright will run every {args.every:g} hours while you are logged in to this Mac.")
    elif args.action == "remove":
        schedule.uninstall()
        print("Schedule removed.")
    else:
        print(schedule.status())


def cmd_status(_args) -> None:
    from jobright import store

    counts = store.status_counts()
    for st in store.STATUSES:
        if counts.get(st):
            print(f"{st:12} {counts[st]}")
    from datetime import date
    print(f"Claude spend this month: ${store.spend(date.today().strftime('%Y-%m')):.2f}")


def cmd_usage(_args) -> None:
    from jobright.store import usage_summary

    rows = usage_summary()
    if not rows:
        print("No Claude API usage recorded yet.")
        return
    print(f"{'month':8} {'purpose':9} {'model':18} {'calls':>5} {'in':>9} {'out':>8} {'cached':>9} {'USD':>8}")
    for month, purpose, model, calls, inp, out, cached, cost in rows:
        print(f"{month:8} {purpose:9} {model:18} {calls:>5} {inp:>9} {out:>8} {cached:>9} {cost:>8.4f}")
    print(f"Total: ${sum(r[-1] for r in rows):.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobright")
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("start", help="open jobright in your browser (starts the dashboard if needed)")
    st.add_argument("--no-browser", action="store_true")
    st.set_defaults(func=cmd_start)
    sub.add_parser("doctor", help="check the install").set_defaults(func=cmd_doctor)
    im = sub.add_parser("import", help="build your master profile from a resume file (PDF / DOCX / text)")
    im.add_argument("file")
    im.set_defaults(func=cmd_import)
    sub.add_parser("plan", help="analyse your profile and set role tracks and searches").set_defaults(func=cmd_plan)
    sub.add_parser("profile", help="validate your master profile").set_defaults(func=cmd_profile)
    sub.add_parser("base", help="render the full master resume to PDF").set_defaults(func=cmd_base)
    sub.add_parser("general", help="two-page resume for your Naukri / LinkedIn profile").set_defaults(func=cmd_general)

    t = sub.add_parser("tailor", help="tailor a resume to one job description")
    t.add_argument("--jd", required=True, help="path to a text file with the JD, or - for stdin")
    t.set_defaults(func=cmd_tailor)

    r = sub.add_parser("run", help="full cycle: discover, screen, tailor, apply approved, report")
    r.add_argument("--sources", help="comma list: ats,linkedin,naukri,reed,indeed (default: your sites)")
    r.add_argument("--no-apply", action="store_true", help="do not submit anything this run")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("discover", help="only find and filter new jobs (no API cost)")
    d.add_argument("--sources", default=None)
    d.set_defaults(func=cmd_discover)

    a = sub.add_parser("apply", help="submit approved jobs")
    a.add_argument("--job", action="append", help="a job id (repeatable); default all approved")
    a.add_argument("--dry-run", action="store_true", help="fill forms but stop before submit")
    a.set_defaults(func=cmd_apply)

    lg = sub.add_parser("login", help="sign in to a site in the bot's browser (once)")
    lg.add_argument("site", choices=["linkedin", "naukri", "reed", "indeed"])
    lg.set_defaults(func=cmd_login)
    sub.add_parser("session", help="are your job-site sessions saved?").set_defaults(func=cmd_session)

    sub.add_parser("dashboard", help="local dashboard at http://127.0.0.1:8765").set_defaults(func=cmd_dashboard)

    sc = sub.add_parser("schedule", help="run automatically (macOS launchd, Windows Task Scheduler, Linux systemd/cron)")
    sc.add_argument("action", choices=["install", "remove", "status"])
    sc.add_argument("--every", type=float, default=3, help="hours between runs (default 3)")
    sc.add_argument("--no-dashboard", action="store_true", help="do not keep the dashboard running")
    sc.set_defaults(func=cmd_schedule)

    sub.add_parser("status", help="job counts and spend").set_defaults(func=cmd_status)
    sub.add_parser("usage", help="Claude API spend by step").set_defaults(func=cmd_usage)

    try:
        sys.stdout.reconfigure(line_buffering=True)   # logs appear as they happen, even redirected
    except Exception:
        pass
    args = parser.parse_args()
    try:
        args.func(args)
    except LLMError as e:
        sys.exit(f"Error: {e}")
