"""Indeed (beta), read in the bot's logged-in browser.

Indeed runs strong bot protection. When it shows its "Just a moment…" /
"verify you are human" check, the bot stops Indeed for that run instead of
trying to get past it, and tries again next run.
"""

from __future__ import annotations

import random
import re
import urllib.parse
from collections.abc import Callable, Iterator

from playwright.sync_api import BrowserContext, Page

from jobright import browser, config
from jobright.apply import guard
from jobright.sources import Posting

_CARDS = r"""
() => [...document.querySelectorAll('[data-jk]')].map(e => {
  const card = e.closest('.job_seen_beacon, .result, li') || e;
  const t = s => (card.querySelector(s)?.innerText || '').trim();
  return {jk: e.getAttribute('data-jk'), title: t('h2 span[title], h2.jobTitle span, h2') || e.innerText.trim(),
          company: t('[data-testid="company-name"], .companyName'), location: t('[data-testid="text-location"], .companyLocation'),
          easy: /easily apply|indeed apply/i.test(card.innerText)};
}).filter((x, i, a) => x.jk && a.findIndex(y => y.jk === x.jk) === i)
"""

_JOB = r"""
() => {
  const t = s => (document.querySelector(s)?.innerText || '').trim();
  const body = document.body.innerText;
  return {desc: t('#jobDescriptionText'), salary: t('#salaryInfoAndJobType, [data-testid="jobsearch-OtherJobDetailsContainer"]'),
          easy: !!document.querySelector('#indeedApplyButton, [data-testid="indeedApplyButton-test"], button[aria-label*="Apply now" i]'),
          applied: /you applied|applied on/i.test(body), closed: /this job has expired|no longer available/i.test(body)};
}
"""


def _domain() -> str:
    return config.country().get("indeed_domain") or "www.indeed.com"


def blocked(pg: Page) -> bool:
    try:
        return "just a moment" in (pg.title() or "").lower() or bool(guard.SECURITY.search(pg.inner_text("body")[:3000]))
    except Exception:
        return False


def discover(ctx: BrowserContext, searches: list[dict], known: Callable[[str], bool],
             max_pages: int = 30, title_ok: Callable[[str], bool] = lambda t: True, log=print) -> Iterator[Posting]:
    pg = browser.page(ctx)
    host = _domain()
    opened = 0
    for s in searches:
        q = urllib.parse.urlencode({"q": s["keywords"], "l": s.get("location", ""), "fromage": 2, "sort": "date"})
        pg.goto(f"https://{host}/jobs?{q}", wait_until="domcontentloaded")
        pg.wait_for_timeout(int(random.uniform(3.0, 5.0) * 1000))
        if blocked(pg):
            log("  indeed: bot check shown; skipping Indeed for this run")
            return
        guard.clear(pg)
        cards = [c for c in pg.evaluate(_CARDS) if not known(c["jk"]) and title_ok(c["title"])]
        log(f"  indeed '{s['keywords']}' / {s.get('location') or ''}: {len(cards)} new with a target title")
        for c in cards:
            if opened >= max_pages:
                return
            url = f"https://{host}/viewjob?jk={c['jk']}"
            pg.goto(url, wait_until="domcontentloaded")
            opened += 1
            pg.wait_for_timeout(int(random.uniform(2.5, 4.5) * 1000))
            if blocked(pg):
                log("  indeed: bot check shown; stopping Indeed for this run")
                return
            d = pg.evaluate(_JOB)
            if d["applied"] or d["closed"] or not d["desc"]:
                continue
            yield Posting(
                source="indeed", external_id=c["jk"], url=url, apply_url=url,
                apply_type="indeed" if d["easy"] else "external",
                title=c["title"], company=c["company"] or "unknown", location=c["location"],
                description=(f"Salary: {d['salary']}\n\n" if d["salary"] else "") + d["desc"],
            )
