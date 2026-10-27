"""LinkedIn job search, read in the bot's logged-in browser.

Searches are limited to the last 24 hours. LinkedIn's current search page
(jobs/search-results) filters location by geoId only and ignores the
experience-level filter, so seniority is left to screening. Pages are opened
one at a time with a pause between them, at the pace of a person reading
results.
"""

from __future__ import annotations

import random
import re
import urllib.parse
from collections.abc import Callable, Iterator

from playwright.sync_api import BrowserContext, Page

from jobright import browser
from jobright.sources import Posting

_SEARCH = "https://www.linkedin.com/jobs/search/?{query}"

# Reads the job page with fallbacks, because LinkedIn renames classes often.
_EXTRACT = r"""
() => {
  const txt = el => (el && (el.innerText || el.textContent) || '').trim();
  const q = s => document.querySelector(s);
  // The page title is "Job title | Company | LinkedIn" on both old and new layouts.
  // Split from the right: a title may itself contain "|" ("Software Architect (.NET | AI)").
  const parts = document.title.replace(/^\(\d+\)\s*/, '').split('|').map(s => s.trim());
  const title = txt(q('h1')) || (parts.length > 2 ? parts.slice(0, -2).join(' | ') : parts[0]);
  let company = txt(q('.job-details-jobs-unified-top-card__company-name')) || (parts.length > 2 ? parts[parts.length - 2] : '');
  if (!company) company = txt(q('main a[href*="/company/"]'));
  // Location is the first part of the "Bengaluru, Karnataka, India · Reposted 14 hours ago · …" line.
  let location = '';
  for (const p of document.querySelectorAll('main p, .job-details-jobs-unified-top-card__primary-description-container')) {
    const t = txt(p);
    if (t.includes('·') && /ago|applicants|clicked apply|people/i.test(t)) { location = t.split('·')[0].trim(); break; }
  }
  const about = q('[id^="JobDetails_AboutTheJob"]');
  const desc = (txt(about).replace(/^About the job\s*/i, '')) || txt(q('#job-details'))
            || txt(q('.jobs-description__content')) || txt(q('.jobs-box__html-content'));
  const buttons = [...document.querySelectorAll('button, a')].map(b => txt(b).toLowerCase());
  const easy = buttons.some(t => t.startsWith('easy apply'));
  const body = document.body.innerText;
  const applied = buttons.some(t => t.startsWith('applied')) || /Applied \d|Application submitted/.test(body);
  const closed = /No longer accepting applications|job posting has been removed|Job id provided may not be valid/i.test(body);
  const top = (txt(q('main')) || body).slice(0, 4000);
  const pay = top.match(/(₹|INR)?\s?[\d.,]+\s?[KMLC]?r?\s?(INR)?\s?\/\s?(yr|year|mo|month)\s?[-–]\s?(₹|INR)?\s?[\d.,]+\s?[KMLC]?r?\s?(INR)?\s?\/\s?(yr|year|mo|month)/i);
  return {title, company, location, desc, easy, applied, closed, salary: pay ? pay[0] : ''};
}
"""


def search_url(keywords: str, geo_id: str | int, hours: int = 24) -> str:
    query = urllib.parse.urlencode({
        "keywords": keywords,
        "geoId": geo_id,
        "f_TPR": f"r{hours * 3600}",   # posted in the last N hours
        "sortBy": "DD",
    })
    return _SEARCH.format(query=query)


def _pause(pg: Page, lo: float = 2.0, hi: float = 4.5) -> None:
    pg.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


_IDS = r"""
() => {
  // New layout: each result card carries componentkey="...-<jobId>". Old layout:
  // data-job-id / data-occludable-job-id. Both: links with /jobs/view/<id> or currentJobId=<id>.
  // Only the results column: the page also carries feed posts and recommendations.
  const scope = document.querySelector('[data-testid="lazy-column"], [data-view-name="lazy-column"]')
             || document.querySelector('.jobs-search-results-list, .scaffold-layout__list') || document;
  const out = [], seen = new Set();
  const add = (id, el) => {
    if (!id || seen.has(id)) return;
    seen.add(id);
    const title = ((el && el.innerText) || '').split('\n')[0]
      .replace(/^Selected,\s*/, '').replace(/\s*\(Verified job\)\s*$/, '').trim();
    out.push([id, title]);
  };
  for (const e of scope.querySelectorAll('[componentkey], [data-job-id], [data-occludable-job-id]')) {
    if (e.closest('button')) continue;
    for (const a of ['componentkey', 'data-job-id', 'data-occludable-job-id']) {
      const m = (e.getAttribute(a) || '').match(/(\d{10,})$/); if (m) add(m[1], e);
    }
  }
  for (const a of scope.querySelectorAll('a[href]')) {
    const m = a.href.match(/(?:currentJobId=|jobs\/view\/)(\d{10,})/); if (m) add(m[1], a);
  }
  return out;
}
"""


def _job_ids(pg: Page) -> list[tuple[str, str]]:
    """(job id, card title) for every result card on the page."""
    for _ in range(6):  # the results list loads as it scrolls
        pg.evaluate("""() => {
            const lists = [...document.querySelectorAll('main div, main ul')].filter(
                e => e.scrollHeight > e.clientHeight + 50 && /auto|scroll/.test(getComputedStyle(e).overflowY));
            (lists.length ? lists : [document.scrollingElement]).forEach(e => e.scrollBy(0, 1200));
        }""")
        pg.wait_for_timeout(800)
    return [tuple(x) for x in pg.evaluate(_IDS)]


def discover(ctx: BrowserContext, searches: list[dict], known: Callable[[str], bool],
             max_pages: int = 40, title_ok: Callable[[str], bool] = lambda t: True,
             log=print) -> Iterator[Posting]:
    """Yield postings not seen before. `known(external_id)` skips stored jobs
    and `title_ok(card_title)` skips off-target titles, both without opening
    the job page."""
    pg = browser.page(ctx)
    if not browser.is_logged_in(ctx, "linkedin"):
        log("  linkedin: not signed in; run `jobright login linkedin`")
        return
    opened = 0
    for s in searches:
        pg.goto(search_url(s["keywords"], s["geo_id"], s.get("hours", 24)), wait_until="domcontentloaded")
        _pause(pg, 3.5, 5.5)
        if "checkpoint" in pg.url or "authwall" in pg.url:
            log("  linkedin: security check shown; stopping LinkedIn for this run")
            return
        cards = [(i, t) for i, t in _job_ids(pg) if not known(i)]
        ids = [i for i, t in cards if not t or title_ok(t)]
        log(f"  linkedin '{s['keywords']}' / {s.get('label', s['geo_id'])}: "
            f"{len(cards)} new, {len(ids)} with a target title")
        for jid in ids:
            if opened >= max_pages:
                return
            url = f"https://www.linkedin.com/jobs/view/{jid}/"
            pg.goto(url, wait_until="domcontentloaded")
            opened += 1
            _pause(pg)
            try:
                pg.get_by_role("button", name=re.compile(r"see more|show more", re.I)).first.click(timeout=1500)
            except Exception:
                pass
            d = pg.evaluate(_EXTRACT)
            if d["applied"] or d["closed"] or not d["desc"]:
                continue
            # LinkedIn shows pay in a pill above the description; keep it with the text
            # so the budget rule sees it.
            desc = (f"Salary: {d['salary']}\n\n" if d["salary"] else "") + d["desc"]
            yield Posting(
                source="linkedin",
                external_id=jid,
                url=url,
                apply_url=url,
                apply_type="easy_apply" if d["easy"] else "external",
                title=d["title"],
                company=d["company"] or "unknown",
                location=d["location"],
                description=desc,
            )
