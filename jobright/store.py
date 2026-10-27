"""Local SQLite store: jobs through their lifecycle, events, and Claude API usage."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from jobright.config import DB_PATH

# Job lifecycle. A job moves left to right; user statuses come after applied.
STATUSES = [
    "discovered",     # found by a source, not yet looked at
    "filtered",       # dropped by title / company / location rules (no API cost)
    "skipped",        # analyzed, below the apply thresholds, or skipped by you
    "duplicate",      # the same job as one already in the pipeline or applied to
    "salary_review",  # budget below your current CTC: you decide
    "screened",       # analyzed, passes thresholds, waiting for tailoring budget
    "ready",          # tailored resume ready, waiting for your approval
    "approved",       # you approved (or auto-submit is on); will be submitted
    "unverified",     # Submit pressed, confirmation not seen yet: re-checked, never re-sent blind
    "applied",        # submitted and confirmed by the site
    "unavailable",    # posting removed, closed or expired
    "needs_you",      # the bot could not finish; see reason / missing questions
    "failed",         # unexpected error; retried up to the attempt limit
    "interview", "rejected", "offer", "withdrawn",
]
ACTIVE = ("discovered", "screened", "ready", "approved", "unverified", "applied", "needs_you",
          "salary_review", "interview", "offer")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    apply_url TEXT,
    apply_type TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    description TEXT,
    posted_at TEXT,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    fit_score INTEGER,
    skill_match INTEGER,
    reason TEXT,
    analysis_json TEXT,
    out_dir TEXT,
    resume_pdf TEXT,
    error TEXT,
    notes TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    job_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT
);
"""

# Columns added after the first release; added in place, data kept.
_MIGRATIONS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_attempt_at": "TEXT",
    "salary_text": "TEXT",
    "budget_max_lpa": "REAL",
    "expected_ctc_override": "REAL",
    "missing_json": "TEXT",
    "evidence": "TEXT",
    "answers_json": "TEXT",
    "years_required": "TEXT",
    "track": "TEXT",                 # role track from settings.tracks
    "priority": "INTEGER",           # 0-100: fit, skills, growth and salary, weighted by track
    "deep_checked": "INTEGER NOT NULL DEFAULT 0",
}


def _now() -> str:
    # Local time with offset, so substr(ts, 1, 10) is the local calendar day
    # that daily limits count against.
    return datetime.now().astimezone().isoformat(timespec="seconds")


class _Connection(sqlite3.Connection):
    """`with connect() as conn:` commits (or rolls back) and then closes.

    sqlite3's own context manager only commits and leaves the connection
    open. Every call then held a file open until garbage collection, and a
    scheduled run (macOS gives launchd jobs 256 open files) died with
    "unable to open database file" after a few hundred updates.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


_migrated_path = None


def connect() -> sqlite3.Connection:
    global _migrated_path
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=_Connection)
    conn.row_factory = sqlite3.Row
    if _migrated_path != DB_PATH:      # schema and column migrations once per process
        conn.executescript(_SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        for col, decl in _MIGRATIONS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        conn.commit()
        _migrated_path = DB_PATH
    return conn


# --- duplicates ----------------------------------------------------------------

_COMPANY_NOISE = re.compile(r"\b(pvt|private|ltd|limited|inc|llp|llc|corp|corporation|technologies|technology|"
                            r"software|solutions|services|systems|labs|india|global|group|the|co)\b")
_TITLE_SWAPS = [(r"\bsr\b", "senior"), (r"\bjr\b", "junior"), (r"\bengg?\b", "engineer"), (r"\bdev\b", "developer"),
                (r"\bmgr\b", "manager"), (r"\bsde\b", "software development engineer"), (r"\bswe\b", "software engineer"),
                (r"\bapp\b", "application"), (r"\bapps\b", "applications")]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", html.unescape(s or "").lower())).strip()


def norm_company(company: str) -> str:
    return " ".join(_COMPANY_NOISE.sub(" ", _norm(company)).split())


def norm_title(title: str) -> str:
    raw = re.sub(r"\[.*?\]", " ", title or "")
    # "Flutter Developer - Bangalore" / "(Hybrid)": a location suffix is not part of the role,
    # but "- Backend" / "(Mobile)" are, so only places and work modes are stripped.
    places = r"(bangalore|bengaluru|hyderabad|pune|mumbai|chennai|noida|gurgaon|gurugram|delhi|india|remote|hybrid|onsite|on-site|wfo|wfh)"
    raw = re.sub(rf"\(\s*{places}[^)]*\)", " ", raw, flags=re.I)
    raw = re.sub(rf"\s+(?:-|–|@|\||in|at)\s+{places}\b.*$", " ", raw, flags=re.I)
    t = _norm(raw)
    for a, b in _TITLE_SWAPS:
        t = re.sub(a, b, t)
    t = re.sub(r"\b(bangalore|bengaluru|hyderabad|pune|mumbai|chennai|india|remote|hybrid|onsite|"
               r"urgent|immediate joiners?|hiring|wfo|wfh|\d+\s*\+?\s*(yrs?|years?))\b", " ", t)
    return " ".join(t.split())


def dedupe_key(company: str, title: str) -> str:
    """Same company + same title on two sites is one job."""
    return f"{norm_company(company)}|{norm_title(title)}"


def _similar(a: str, b: str) -> float:
    x, y = set(a.split()), set(b.split())
    return len(x & y) / len(x | y) if x and y else 0.0


def find_duplicate(company: str, title: str, exclude_id: str | None = None,
                   statuses: tuple[str, ...] = ACTIVE) -> dict | None:
    """A job already in the pipeline that is the same opening: same company, and
    the same or a near-identical title (token overlap ≥ 0.8)."""
    nc, nt = norm_company(company), norm_title(title)
    if not nc:
        return None
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))}) AND id != ?",
            (*statuses, exclude_id or ""),
        ).fetchall()
    for r in rows:
        rc = norm_company(r["company"])
        if rc != nc and not (rc and (rc in nc or nc in rc)):
            continue
        rt = norm_title(r["title"])
        if rt == nt or _similar(rt, nt) >= 0.8:
            return dict(r)
    return None


def applications_to_company(company: str, days: int = 30) -> int:
    since = (date.today() - timedelta(days=days)).isoformat()
    nc = norm_company(company)
    with connect() as conn:
        rows = conn.execute("SELECT company FROM jobs WHERE applied_at IS NOT NULL AND substr(applied_at,1,10) >= ?",
                            (since,)).fetchall()
    return sum(1 for r in rows if norm_company(r[0]) == nc)


# --- jobs ----------------------------------------------------------------------

def add_job(*, source: str, external_id: str, url: str, title: str, company: str,
            location: str = "", description: str = "", apply_url: str | None = None,
            apply_type: str | None = None, posted_at: str | None = None) -> bool:
    """Insert a newly discovered job. Returns False if it was already known,
    either from this source or as the same job on another site."""
    title, company = html.unescape(title).strip(), html.unescape(company).strip()
    jid = f"{source}:{external_id}"
    key = dedupe_key(company, title)
    with connect() as conn:
        if conn.execute("SELECT 1 FROM jobs WHERE id=? OR dedupe_key=?", (jid, key)).fetchone():
            return False
    if find_duplicate(company, title, statuses=tuple(STATUSES)):
        return False
    with connect() as conn:
        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, source, external_id, url, apply_url, apply_type, title, company,"
            " location, description, posted_at, dedupe_key, discovered_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, source, external_id, url, apply_url, apply_type, title, company, location,
             description, posted_at, key, now, now),
        )
        return True


def seen(company: str, title: str) -> bool:
    """Already stored as this company + title, from any source."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM jobs WHERE dedupe_key=?", (dedupe_key(company, title),)).fetchone():
            return True
    return find_duplicate(company, title, statuses=tuple(STATUSES)) is not None


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    if fields.get("status") == "applied":
        fields.setdefault("applied_at", _now())
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    if "status" in fields:
        log_event(job_id, "status", fields["status"])


def bump_attempts(job_id: str) -> int:
    with connect() as conn:
        conn.execute("UPDATE jobs SET attempts = attempts + 1, last_attempt_at=? WHERE id=?", (_now(), job_id))
        return conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()[0]


def get_job(job_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def jobs(status: str | list[str] | None = None, source: str | None = None,
         limit: int = 500, order: str = "updated_at DESC") -> list[dict]:
    where, args = [], []
    if status:
        statuses = [status] if isinstance(status, str) else status
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args += statuses
    if source:
        where.append("source=?")
        args.append(source)
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
    sql += f" ORDER BY {order} LIMIT ?"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (*args, limit)).fetchall()]


def status_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}


def tailored_today() -> int:
    today = date.today().isoformat()
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='tailored' AND substr(ts,1,10)=?", (today,)
        ).fetchone()[0]


def applied_today(source: str) -> int:
    today = date.today().isoformat()
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE source=? AND applied_at IS NOT NULL AND substr(applied_at,1,10)=?",
            (source, today),
        ).fetchone()[0]


def log_event(job_id: str | None, kind: str, detail: Any = None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail)
    with connect() as conn:
        conn.execute("INSERT INTO events (ts, job_id, kind, detail) VALUES (?,?,?,?)",
                     (_now(), job_id, kind, detail))


def recent_events(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT e.*, j.title, j.company FROM events e LEFT JOIN jobs j ON j.id = e.job_id"
            " ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()]


def record_usage(purpose: str, model: str, input_tokens: int, output_tokens: int,
                 cache_read: int, cache_write: int, cost_usd: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO llm_usage (ts, purpose, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
            (_now(), purpose, model, input_tokens, output_tokens, cache_read, cache_write, cost_usd),
        )


def usage_summary() -> list[tuple]:
    with connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT substr(ts, 1, 7) AS month, purpose, model, COUNT(*),"
            " SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), ROUND(SUM(cost_usd), 4)"
            " FROM llm_usage GROUP BY month, purpose, model ORDER BY month DESC, purpose"
        ).fetchall()]


def spend(period: str) -> float:
    """USD spent in a period prefix: '2026-10' for a month, '2026-10-24' for a day."""
    with connect() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE substr(ts,1,?)=?",
            (len(period), period),
        ).fetchone()[0]
