"""Reed (UK) through its official Jobseeker API.

Needs a free API key from https://www.reed.co.uk/developers/jobseeker, stored as
REED_API_KEY in your jobright .env (the setup wizard asks for it). Searching
needs no login; applying happens on reed.co.uk in the bot's browser.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

from jobright.sources import Posting, html_to_text

_API = "https://www.reed.co.uk/api/1.0"


def _get(path: str, key: str) -> dict:
    auth = base64.b64encode(f"{key}:".encode()).decode()
    req = urllib.request.Request(_API + path, headers={"Authorization": f"Basic {auth}",
                                                       "User-Agent": "jobright (personal job search)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _posted(d: str | None) -> datetime | None:
    try:
        return datetime.strptime(d or "", "%d/%m/%Y")
    except ValueError:
        return None


def discover(searches: list[dict], known: Callable[[str], bool], title_ok: Callable[[str], bool] = lambda t: True,
             max_age_days: int = 2, max_details: int = 60, log=print) -> Iterator[Posting]:
    key = os.environ.get("REED_API_KEY")
    if not key:
        log("  reed: no REED_API_KEY; add it in Setup (free from reed.co.uk/developers)")
        return
    since = datetime.now() - timedelta(days=max_age_days + 1)
    fetched = 0
    for s in searches:
        q = urllib.parse.urlencode({"keywords": s["keywords"], "locationName": s.get("location", ""),
                                    "distanceFromLocation": s.get("distance", 20), "resultsToTake": 100})
        try:
            results = _get(f"/search?{q}", key).get("results", [])
        except Exception as e:
            log(f"  reed '{s['keywords']}': {e}")
            continue
        fresh = [r for r in results if (_posted(r.get("date")) or datetime.now()) >= since
                 and not known(str(r["jobId"])) and title_ok(r.get("jobTitle", ""))]
        log(f"  reed '{s['keywords']}' / {s.get('location') or 'UK'}: {len(results)} results, {len(fresh)} new and recent")
        for r in fresh:
            if fetched >= max_details:
                return
            fetched += 1
            try:
                d = _get(f"/jobs/{r['jobId']}", key)
            except Exception:
                d = r
            lo, hi = d.get("minimumSalary") or r.get("minimumSalary"), d.get("maximumSalary") or r.get("maximumSalary")
            pay = f"Salary: £{lo:,.0f} - £{hi:,.0f} per annum\n\n" if lo and hi else (f"Salary: £{hi or lo:,.0f} per annum\n\n" if (hi or lo) else "")
            url = d.get("jobUrl") or r.get("jobUrl") or f"https://www.reed.co.uk/jobs/{r['jobId']}"
            yield Posting(
                source="reed",
                external_id=str(r["jobId"]),
                url=url,
                apply_url=url,
                apply_type="reed" if not d.get("externalUrl") else "external",
                title=r.get("jobTitle", ""),
                company=r.get("employerName") or "unknown",
                location=r.get("locationName", ""),
                description=pay + html_to_text(d.get("jobDescription") or r.get("jobDescription") or ""),
                posted_at=r.get("date"),
            )
