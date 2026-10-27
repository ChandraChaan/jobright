"""Daily report: what was found, applied, waiting on you, and what it cost."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobright import config, store

REPORTS_DIR = config.DATA / "reports"
_env = Environment(loader=FileSystemLoader(config.TEMPLATES_DIR), autoescape=select_autoescape(["html", "j2"]))


def daily_data(day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    with store.connect() as conn:
        found = conn.execute("SELECT COUNT(*) FROM jobs WHERE substr(discovered_at,1,10)=?", (day,)).fetchone()[0]
        applied = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE substr(applied_at,1,10)=? ORDER BY applied_at", (day,)).fetchall()]
        by_source = conn.execute(
            "SELECT source, COUNT(*) FROM jobs WHERE substr(applied_at,1,10)=? GROUP BY source", (day,)).fetchall()
    return {
        "day": day,
        "found": found,
        "applied": applied,
        "by_source": {r[0]: r[1] for r in by_source},
        "ready": store.jobs("ready", limit=100),
        "needs_you": store.jobs("needs_you", limit=100),
        "failed": store.jobs("failed", limit=50),
        "counts": store.status_counts(),
        "spend_day": store.spend(day),
        "spend_month": store.spend(day[:7]),
        "budget": config.settings()["limits"]["monthly_budget_usd"],
    }


def write_daily(day: str | None = None) -> Path:
    data = daily_data(day)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{data['day']}.html"
    path.write_text(_env.get_template("report.html.j2").render(**data))
    return path
