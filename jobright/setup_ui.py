"""The setup wizard: from a fresh install to a running job search, in the browser.

Mounted on the dashboard at /setup. Each step saves as it goes, so a user can
stop and come back; the overview shows what is done.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from jobright import config

router = APIRouter(prefix="/setup")
REVIEW_PATH = config.DATA / "import_review.yaml"
RESUME_DIR = config.DATA / "resume"

# Answer-bank fields, in the order the form shows them. `countries` limits a
# field to some countries; `kind` drives the input.
ANSWER_FIELDS = [
    {"key": "total_experience_years", "label": "Total years of professional experience", "kind": "number", "step": "0.1",
     "help": "Internships count only if you want them to. Never rounded up on forms."},
    {"key": "current_salary", "label": "Current salary (annual)", "kind": "number", "salary": True,
     "help": "Leave empty if you are a student or not working."},
    {"key": "expected_salary", "label": "Expected salary (annual)", "kind": "number", "salary": True,
     "help": "Used unless a job's budget is lower; see the salary rule below."},
    {"key": "notice_period_days", "label": "Notice period (days)", "kind": "number", "help": "0 = can start immediately."},
    {"key": "currently_serving_notice", "label": "Currently serving notice?", "kind": "yesno"},
    {"key": "current_location", "label": "Current city", "kind": "text"},
    {"key": "willing_to_relocate", "label": "Willing to relocate?", "kind": "yesno"},
    {"key": "open_to_remote", "label": "Open to remote work?", "kind": "yesno"},
    {"key": "open_to_hybrid", "label": "Open to hybrid work?", "kind": "yesno"},
    {"key": "open_to_onsite", "label": "Open to working on site / in office?", "kind": "yesno"},
    {"key": "work_authorization", "label": "Right to work in your country", "kind": "text",
     "help_country": "work_authorization_example"},
    {"key": "needs_sponsorship", "label": "Do you need visa sponsorship to work in your country (now or later)?", "kind": "yesno"},
    {"key": "needs_visa_sponsorship_abroad", "label": "Would you need sponsorship for jobs in other countries?", "kind": "yesno"},
    {"key": "highest_education", "label": "Highest qualification", "kind": "text", "help": "e.g. BSc Computer Science, University of Leeds"},
    {"key": "graduation_year", "label": "Graduation year", "kind": "number"},
    {"key": "degree_grade", "label": "Degree classification / grade", "kind": "text", "countries": ["GB"],
     "help": "e.g. First, 2:1, 2:2"},
    {"key": "english_proficiency", "label": "English proficiency", "kind": "select",
     "options": ["Native or bilingual", "Full professional", "Professional working", "Limited working", "Elementary"]},
    {"key": "has_passport", "label": "Do you hold a valid passport?", "kind": "yesno"},
    {"key": "willing_to_travel", "label": "Willing to travel for work?", "kind": "yesno"},
    {"key": "driving_licence", "label": "Do you hold a full driving licence?", "kind": "yesno", "countries": ["GB"]},
    {"key": "gender", "label": "Gender (optional; empty = decline where the form allows)", "kind": "text"},
    {"key": "date_of_birth", "label": "Date of birth (optional)", "kind": "text", "help": "e.g. 15 Aug 2002. Empty = asked per form."},
]


def _env() -> "Environment":
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(loader=FileSystemLoader(config.TEMPLATES_DIR), autoescape=select_autoescape(["html", "j2"]))
    env.filters["tojson"] = lambda v: json.dumps(v)
    return env


def _page(name: str, **ctx) -> HTMLResponse:
    from jobright import store
    ctx.setdefault("nav_counts", store.status_counts())
    ctx.setdefault("status", status())
    ctx.setdefault("country", config.country())
    return HTMLResponse(_env().get_template(f"setup/{name}").render(**ctx))


def _load_yaml(path: Path):
    if not path.exists():
        return None
    with path.open() as f:
        return yaml.safe_load(f)


def status() -> dict[str, bool]:
    s = config.settings()
    prof = s.get("profile") or {}
    return {
        "key": bool(os.environ.get("ANTHROPIC_API_KEY") or (s.get("ai") or {}).get("chosen")),
        "resume": config.PROFILE_PATH.exists(),
        "country": bool(prof.get("country")),
        "plan": bool(s.get("tracks")),
        "answers": config.ANSWERS_PATH.exists(),
        "logins": bool(prof.get("logins_checked")),
        "done": bool(prof.get("setup_complete")),
    }


def setup_needed() -> bool:
    return not status()["done"]


def _spawn(*args: str) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = (config.LOGS_DIR / "setup.log").open("a")
    subprocess.Popen([sys.executable, "-m", "jobright", *args], stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True, env={**os.environ, "JOBRIGHT_HOME": str(config.DATA)})


# --- steps ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def overview():
    return _page("overview.html.j2")


@router.get("/key", response_class=HTMLResponse)
def key_page(error: str = ""):
    from jobright import engine
    engine.ollama_models.cache_clear()
    ai = config.settings().get("ai") or {}
    return _page("key.html.j2", error=error, has_reed=bool(os.environ.get("REED_API_KEY")),
                 has_key=bool(os.environ.get("ANTHROPIC_API_KEY")), choice=ai.get("engine") or "auto",
                 ollama=engine.ollama_models(), local_model=engine.local_model(), now=engine.describe())


@router.post("/key")
def key_save(engine_choice: str = Form("auto"), anthropic_key: str = Form(""), reed_key: str = Form(""),
             local_model: str = Form("")):
    ai = {"engine": engine_choice if engine_choice in ("auto", "free", "local", "claude") else "auto", "chosen": True}
    if local_model.strip():
        ai["local_model"] = local_model.strip()
    config.save_settings({"ai": ai})
    if anthropic_key.strip():
        import anthropic
        try:
            anthropic.Anthropic(api_key=anthropic_key.strip(), max_retries=1).models.list(limit=1)
        except anthropic.AuthenticationError:
            return RedirectResponse("/setup/key?error=That+Anthropic+key+was+rejected", status_code=303)
        except Exception:
            pass   # offline or rate limited: keep the key, the first run will tell
        config.set_env("ANTHROPIC_API_KEY", anthropic_key.strip())
    if reed_key.strip():
        config.set_env("REED_API_KEY", reed_key.strip())
    return RedirectResponse("/setup", status_code=303)


@router.get("/resume", response_class=HTMLResponse)
def resume_page(error: str = ""):
    return _page("resume.html.j2", error=error)


@router.post("/resume")
async def resume_upload(file: UploadFile = File(...)):
    from jobright import onboard
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "resume.pdf").suffix.lower() or ".pdf"
    if suffix not in (".pdf", ".docx", ".txt", ".md"):
        return RedirectResponse("/setup/resume?error=Please+upload+a+PDF,+Word+(.docx)+or+text+file", status_code=303)
    path = RESUME_DIR / f"original{suffix}"
    path.write_bytes(await file.read())
    try:
        profile, flagged, _ = onboard.import_resume(path)
    except Exception as e:
        return RedirectResponse(f"/setup/resume?error={str(e)[:200]}", status_code=303)
    onboard.save_profile(profile)
    REVIEW_PATH.write_text(yaml.safe_dump([{"id": i, "text": t} for i, t in flagged], allow_unicode=True))
    stem = "_".join(profile.contact.name.split()[:2]) or "Resume"
    config.save_settings({"resume": {"file_stem": stem}})
    return RedirectResponse("/setup/profile", status_code=303)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(error: str = ""):
    from jobright.profile import load_profile
    if not config.PROFILE_PATH.exists():
        return RedirectResponse("/setup/resume", status_code=303)
    return _page("profile.html.j2", profile=load_profile(), raw=config.PROFILE_PATH.read_text(),
                 flagged=_load_yaml(REVIEW_PATH) or [], error=error)


@router.post("/profile")
def profile_save(raw: str = Form(...)):
    from jobright.profile import Profile
    try:
        Profile.model_validate(yaml.safe_load(raw))
    except Exception as e:
        return RedirectResponse(f"/setup/profile?error={str(e)[:300]}", status_code=303)
    config.PROFILE_PATH.write_text(raw)
    if REVIEW_PATH.exists():
        REVIEW_PATH.unlink()
    return RedirectResponse("/setup", status_code=303)


@router.get("/country", response_class=HTMLResponse)
def country_page():
    prof = config.settings().get("profile") or {}
    return _page("country.html.j2", countries=config.countries(), prof=prof,
                 excluded=", ".join(config.settings().get("exclude_companies") or []))


@router.post("/country")
async def country_save(request: Request):
    form = await request.form()
    code = str(form.get("country") or "OTHER")
    cities = [str(c) for c in form.getlist("cities")] + [c.strip() for c in str(form.get("other_cities") or "").split(",") if c.strip()]
    sites = [str(s) for s in form.getlist("sites")]
    excluded = [c.strip() for c in str(form.get("exclude_companies") or "").split(",") if c.strip()]
    config.save_settings({"profile": {"country": code, "cities": cities, "sites": sites,
                                      "remote_ok": bool(form.get("remote_ok"))},
                          "exclude_companies": excluded})
    return RedirectResponse("/setup/plan", status_code=303)


@router.get("/plan", response_class=HTMLResponse)
def plan_page(error: str = ""):
    from jobright.onboard import load_career
    return _page("plan.html.j2", plan=load_career(), tracks=config.settings().get("tracks") or {}, error=error)


@router.post("/plan/analyze")
def plan_analyze():
    from jobright import onboard
    from jobright.profile import load_profile
    try:
        plan = onboard.plan_career(load_profile(), config.country().get("name", "your country"))
    except Exception as e:
        return RedirectResponse(f"/setup/plan?error={str(e)[:200]}", status_code=303)
    onboard.save_career(plan)
    return RedirectResponse("/setup/plan", status_code=303)


@router.post("/plan")
async def plan_save(request: Request):
    from jobright import onboard
    form = await request.form()
    plan = onboard.load_career()
    if plan is None:
        return RedirectResponse("/setup/plan", status_code=303)
    keep = []
    for t in plan.tracks:
        if not form.get(f"use::{t.name}"):
            continue
        t.titles = [x.strip() for x in str(form.get(f"titles::{t.name}") or "").split(",") if x.strip()] or t.titles
        t.search_keywords = [x.strip() for x in str(form.get(f"kw::{t.name}") or "").split(",") if x.strip()] or t.search_keywords
        keep.append(t)
    plan.tracks = keep
    onboard.save_career(plan)
    prof = config.settings().get("profile") or {}
    onboard.apply_plan(plan, prof.get("country") or "OTHER", prof.get("cities") or [],
                       prof.get("sites") or ["linkedin", "ats"], config.settings().get("exclude_companies") or [],
                       prof.get("remote_ok", True))
    return RedirectResponse("/setup/answers", status_code=303)


@router.get("/answers", response_class=HTMLResponse)
def answers_page():
    from jobright.answers import bank
    code = config.country()["code"]
    fields = [f for f in ANSWER_FIELDS if not f.get("countries") or code in f["countries"]]
    b = bank()
    skills = (b.get("relevant_experience_years") or {})
    if not skills:
        from jobright.onboard import load_career
        plan = load_career()
        skills = {s.skill: "" for s in (plan.strengths if plan else [])[:10]}
    return _page("answers.html.j2", fields=fields, bank=b, skills=skills)


@router.post("/answers")
async def answers_save(request: Request):
    form = await request.form()
    b = {}
    for f in ANSWER_FIELDS:
        v = str(form.get(f["key"]) or "").strip()
        if f["kind"] == "number" and v:
            try:
                v = float(v) if "." in v else int(v)
            except ValueError:
                pass
        if f["kind"] == "yesno" and v:
            v = v == "yes"
        b[f["key"]] = v if v != "" else None
    b["salary_unit"] = "lpa" if config.country().get("salary_style") == "lpa" else "annual"
    b["currency"] = config.country().get("currency")
    rel = {}
    for i in range(40):   # rows: skillname::i / skillyears::i
        name = str(form.get(f"skillname::{i}") or "").strip()
        years = str(form.get(f"skillyears::{i}") or "").strip()
        if name and years:
            try:
                rel[name] = float(years)
            except ValueError:
                pass
    b["relevant_experience_years"] = rel
    b["preferred_locations"] = (config.settings().get("profile") or {}).get("cities") or []
    header = ("# Answers for application forms. The bot answers form questions only from here,\n"
              "# your profile and answers you save on the Questions page. Empty = it asks you.\n\n")
    config.ANSWERS_PATH.write_text(header + yaml.safe_dump(b, sort_keys=False, allow_unicode=True))
    return RedirectResponse("/setup/logins", status_code=303)


@router.get("/logins", response_class=HTMLResponse)
def logins_page():
    sites = [s for s in ((config.settings().get("profile") or {}).get("sites") or []) if s in ("linkedin", "naukri", "reed", "indeed")]
    return _page("logins.html.j2", sites=sites)


@router.post("/logins/{site}")
def login_start(site: str):
    if site in ("linkedin", "naukri", "reed", "indeed"):
        _spawn("login", site)
    return RedirectResponse("/setup/logins", status_code=303)


@router.post("/logins-done")
def logins_done():
    config.save_settings({"profile": {"logins_checked": True}})
    return RedirectResponse("/setup/finish", status_code=303)


@router.get("/finish", response_class=HTMLResponse)
def finish_page():
    return _page("finish.html.j2", s=config.settings())


@router.post("/finish")
async def finish_save(request: Request):
    form = await request.form()
    every = float(str(form.get("every_hours") or 3))
    budget = float(str(form.get("monthly_budget_usd") or 60))
    auto = {site: bool(form.get(f"auto::{site}")) for site in ("linkedin", "naukri", "reed", "indeed", "ats")}
    config.save_settings({"profile": {"setup_complete": True}, "schedule": {"every_hours": every},
                          "limits": {"monthly_budget_usd": budget}, "apply": {"auto_submit": auto}})
    if form.get("schedule"):
        _spawn("schedule", "install", "--every", str(every))
    if form.get("run_now"):
        _spawn("run")
    return RedirectResponse("/", status_code=303)
