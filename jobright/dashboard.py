"""Local dashboard: http://127.0.0.1:8765 — pipeline, review queue, questions, spend."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobright import answers, config, engine, learn, store
from jobright.report import daily_data, write_daily

app = FastAPI(title="jobright")

from jobright.setup_ui import router as setup_router, setup_needed  # noqa: E402

app.include_router(setup_router)
_env = Environment(loader=FileSystemLoader(config.TEMPLATES_DIR / "dash"), autoescape=select_autoescape(["html", "j2"]))
_env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
LOGS = config.DATA / "logs"

USER_STATUSES = ["interview", "rejected", "offer", "withdrawn", "applied", "skipped", "unavailable"]


def _page(name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("nav_counts", store.status_counts())
    return HTMLResponse(_env.get_template(name).render(**ctx))


def _spawn(*args: str) -> None:
    """Run a jobright command in the background, logging to data/logs/."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{date.today()}.log").open("a")
    subprocess.Popen([sys.executable, "-m", "jobright", *args], cwd=config.DATA,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


@app.get("/", response_class=HTMLResponse)
def overview():
    if setup_needed():
        return RedirectResponse("/setup", status_code=303)
    s = config.settings()
    limits = s["limits"]
    applied = {src: store.applied_today(src) for src in ("linkedin", "naukri", "reed", "indeed", "greenhouse", "lever", "ashby")}
    sites = [x for x in ((s.get("profile") or {}).get("sites") or ["linkedin", "naukri"]) if x in ("linkedin", "naukri", "reed", "indeed")]
    return _page(
        "overview.html.j2",
        data=daily_data(),
        applied=applied,
        limits=limits,
        events=store.recent_events(30),
        auto=s["apply"]["auto_submit"],
        sites=sites,
        rules=s["apply_rules"],
        pending=len(answers.pending_questions()),
        running=_running(),
        tracks=learn.summary(),
        engine=engine.describe(),
    )


def _running() -> bool:
    from jobright.lock import is_held
    from jobright.runner import LOCK_PATH
    return is_held(LOCK_PATH)


@app.get("/jobs", response_class=HTMLResponse)
def jobs(status: str | None = None, source: str | None = None):
    statuses = status.split(",") if status else None
    order = "priority DESC, updated_at DESC" if statuses and set(statuses) & {"ready", "screened", "approved"} else "updated_at DESC"
    return _page("jobs.html.j2", jobs=store.jobs(statuses, source, limit=1000, order=order), status=status, source=source)


@app.get("/jobs/{job_id:path}/resume.pdf")
def resume_pdf(job_id: str):
    job = store.get_job(job_id)
    if not job or not job.get("resume_pdf") or not Path(job["resume_pdf"]).exists():
        raise HTTPException(404)
    return FileResponse(job["resume_pdf"], media_type="application/pdf")


@app.get("/shot")
def shot(path: str):
    p = Path(path).resolve()
    if not str(p).startswith(str((config.DATA / "screenshots").resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


@app.get("/jobs/{job_id:path}", response_class=HTMLResponse)
def job_detail(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404)
    report = {}
    if job.get("out_dir") and (Path(job["out_dir"]) / "report.json").exists():
        report = json.loads((Path(job["out_dir"]) / "report.json").read_text())
    with store.connect() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchall()]
    shots = [json.loads(e["detail"]).get("screenshot") for e in events if e["kind"] == "apply" and e["detail"]]
    return _page("job.html.j2", job=job, report=report, events=events,
                 shots=[s for s in shots if s], user_statuses=USER_STATUSES)


@app.post("/jobs/{job_id:path}/approve")
def approve(job_id: str, now: str = Form("")):
    store.update_job(job_id, status="approved")
    if now:
        _spawn("apply", "--job", job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id:path}/status")
def set_status(job_id: str, status: str = Form(...), notes: str = Form("")):
    if status not in USER_STATUSES + ["ready", "screened"]:
        raise HTTPException(400)
    fields = {"status": status}
    if notes:
        fields["notes"] = notes
    store.update_job(job_id, **fields)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id:path}/salary")
def set_salary(job_id: str, expected_ctc: float = Form(...)):
    """Approve a salary-review job with the expected CTC you choose for it."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404)
    nxt = "approved" if job.get("resume_pdf") else "screened"
    store.update_job(job_id, expected_ctc_override=expected_ctc, status=nxt,
                     reason=f"you set expected CTC {expected_ctc:g} LPA for this job")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/approve-ready")
def approve_ready(now: str = Form("")):
    for job in store.jobs("ready", limit=1000):
        store.update_job(job["id"], status="approved")
    if now:
        _spawn("apply")
    return RedirectResponse("/", status_code=303)


@app.post("/run")
def run_now(sources: str = Form("")):
    _spawn("run", *(["--sources", sources] if sources else []))
    return RedirectResponse("/", status_code=303)


@app.get("/questions", response_class=HTMLResponse)
def questions():
    return _page("questions.html.j2", pending=answers.pending_questions(), saved=answers.saved())


@app.post("/questions")
async def save_questions(request: Request):
    form = await request.form()
    for key, value in form.items():
        if key.startswith("q::") and str(value).strip():
            answers.save_answer(key[3:], str(value).strip())
    return RedirectResponse("/questions", status_code=303)


@app.get("/report", response_class=HTMLResponse)
def report():
    return HTMLResponse(write_daily().read_text())


@app.get("/logs", response_class=HTMLResponse)
def logs():
    path = LOGS / f"{date.today()}.log"
    text = path.read_text()[-20000:] if path.exists() else "No log yet today."
    return _page("logs.html.j2", text=text)


def serve() -> None:
    import uvicorn
    port = config.settings().get("dashboard", {}).get("port", 8765)
    print(f"Dashboard: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
