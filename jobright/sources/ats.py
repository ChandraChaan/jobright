"""Company career boards with public JSON APIs: Greenhouse, Lever, Ashby.

No login and no scraping: these are the feeds the companies publish for their
own career pages.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone

from jobright.sources import Posting, html_to_text

_UA = {"User-Agent": "jobright/0.1 (personal job search)"}


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=30) as r:
        return json.load(r)


def greenhouse(board: str) -> Iterator[Posting]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    for j in data.get("jobs", []):
        yield Posting(
            source="greenhouse",
            external_id=f"{board}-{j['id']}",
            url=j["absolute_url"],
            apply_url=j["absolute_url"],
            apply_type="greenhouse",
            title=j["title"],
            company=j.get("company_name") or board,
            location=(j.get("location") or {}).get("name", ""),
            description=html_to_text(j.get("content", "")),
            posted_at=j.get("first_published") or j.get("updated_at"),
        )


def lever(company: str) -> Iterator[Posting]:
    for j in _get(f"https://api.lever.co/v0/postings/{company}?mode=json"):
        cats = j.get("categories") or {}
        parts = [j.get("openingPlain") or "", j.get("descriptionPlain") or ""]
        for block in j.get("lists") or []:
            parts.append(block.get("text", "") + "\n" + html_to_text(block.get("content", "")))
        parts.append(j.get("additionalPlain") or "")
        created = j.get("createdAt")
        yield Posting(
            source="lever",
            external_id=f"{company}-{j['id']}",
            url=j["hostedUrl"],
            apply_url=j.get("applyUrl") or j["hostedUrl"] + "/apply",
            apply_type="lever",
            title=j["text"],
            company=company.capitalize(),
            location=", ".join(cats.get("allLocations") or [cats.get("location") or ""]),
            description="\n\n".join(p.strip() for p in parts if p.strip()),
            posted_at=datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else None,
        )


def ashby(board: str) -> Iterator[Posting]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        yield Posting(
            source="ashby",
            external_id=f"{board}-{j['id']}",
            url=j["jobUrl"],
            apply_url=j.get("applyUrl") or j["jobUrl"],
            apply_type="ashby",
            title=j["title"],
            company=board.capitalize(),
            location=", ".join(l for l in locs if l),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            posted_at=j.get("publishedAt"),
        )


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def discover(boards: dict[str, list[str]], log=print) -> Iterator[Posting]:
    for kind, names in (boards or {}).items():
        fetch = FETCHERS.get(kind)
        if not fetch:
            continue
        for name in names or []:
            try:
                yield from fetch(name)
            except Exception as e:  # one broken board must not stop the run
                log(f"  {kind}/{name}: {e}")
