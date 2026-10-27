"""Naukri job search, read in the bot's logged-in browser.

Naukri's TopTier layout (what a logged-in TopTier account sees) has no job
links in the results: each card opens the job in a new tab when clicked. With
a jobAge filter it also switches to personalized "jobs for you" and stops
matching the keyword, so searches go without it and recency is read from each
card ("1d ago"). Off-target titles and old jobs are skipped from the card
alone, before any job page is opened.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterator

from playwright.sync_api import BrowserContext, Page

from jobright import browser, store
from jobright.sources import Posting

_CARDS = "div.cursor-pointer.rounded-3xl"

# Reads a job page; TopTier first, then the classic layout's hashed classes.
_EXTRACT = r"""
() => {
  const txt = el => (el && (el.innerText || el.textContent) || '').trim();
  const q = s => document.querySelector(s);
  const leaf = label => [...document.querySelectorAll('*')].find(e => e.children.length === 0 && e.textContent.trim() === label);
  const lines = document.body.innerText.split('\n').map(s => s.trim()).filter(Boolean);

  let company = txt(q('[class*="jd-header-comp-name"] a')) || txt(q('main h4, h4'));
  const ci = lines.indexOf(company);
  let title = txt(q('h1')) || (ci >= 0 ? lines[ci + 1] : '');
  let location = txt(q('[class*="jhc__location"]')) || (ci >= 0 ? lines[ci + 2] : '');
  const header = ci >= 0 ? lines.slice(ci + 1, ci + 7).filter(l => !/ago$|^Quick apply$|^Applied$/.test(l)).join('\n') : '';

  const jd = leaf('Job Description');
  let desc = jd ? txt(jd.parentElement).replace(/^Job Description\s*/, '') : '';
  if (!desc) desc = txt(q('[class*="JDC__dang-inner-html"]')) || txt(q('section[class*="job-desc"]'));
  const ex = leaf('Expertise');
  const skills = ex ? txt(ex.parentElement).split('\n').map(s => s.trim()).filter(s => s && s !== 'Expertise')
                    : [...document.querySelectorAll('[class*="key-skill"] a, [class*="key-skill"] span')].map(txt).filter(Boolean);

  // Buttons animate between labels ("Quick apply" -> "Applied"); only the label in view counts.
  // A label's own text nodes, so "Applied" next to a check-mark icon still counts.
  const own = el => [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
  const labels = [];
  for (const b of document.querySelectorAll('button')) {
    const br = b.getBoundingClientRect();
    for (const k of [b, ...b.querySelectorAll('span, div')]) {
      const t = own(k);
      if (!t) continue;
      const r = k.getBoundingClientRect();
      if (r.width && r.top >= br.top - 1 && r.bottom <= br.bottom + 1) labels.push(t.toLowerCase());
    }
  }
  const companySite = labels.some(t => t.includes('company site'));
  const quick = labels.some(t => t === 'quick apply' || t === 'apply');
  const applied = labels.some(t => t === 'applied');
  return {title, company, location, header, desc, skills, companySite, quick, applied};
}
"""

# Each result card's lines: company, [rating, industry…], "<age>[ - On company site]",
# ["Quick apply"], title, location, salary, skills, experience.
_CARD_LINES = "() => [...document.querySelectorAll('" + _CARDS + "')].map(c => c.innerText.split('\\n').map(s => s.trim()).filter(Boolean))"


def search_url(keywords: str, location: str) -> str:
    kw = re.sub(r"[^a-z0-9]+", "-", keywords.lower()).strip("-")
    loc = re.sub(r"[^a-z0-9]+", "-", location.lower()).strip("-")
    return f"https://www.naukri.com/{kw}-jobs-in-{loc}"


def age_days(text: str) -> int:
    t = text.lower()
    if re.search(r"just now|few hours|hours? ago|today|minute", t):
        return 0
    m = re.search(r"(\d+)\+?\s*d", t)
    return int(m.group(1)) if m else 99


def parse_card(lines: list[str]) -> dict | None:
    i = next((k for k, l in enumerate(lines) if re.search(r"\bago\b", l, re.I)), None)
    if i is None:
        return None
    j = i + 1
    quick = j < len(lines) and lines[j].lower() == "quick apply"
    if quick:
        j += 1
    if j >= len(lines):
        return None
    company = lines[0]
    if company.lower().startswith("hiring for") and len(lines) > 1 and lines[1].lower().startswith("posted by"):
        company = lines[1][len("posted by"):].strip()   # a recruiter posting for an unnamed client
    return {
        "company": company,
        "title": lines[j],
        "location": lines[j + 1] if j + 1 < len(lines) else "",
        "age": age_days(lines[i]),
        "company_site": "company site" in lines[i].lower(),
        "quick": quick,
    }


def _pause(pg: Page, lo: float = 2.0, hi: float = 4.5) -> None:
    pg.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


def _load_cards(pg: Page, want: int = 80) -> list[list[str]]:
    for _ in range(8):
        cards = pg.evaluate(_CARD_LINES)
        if len(cards) >= want:
            break
        pg.mouse.wheel(0, 4000)
        pg.wait_for_timeout(1200)
    return pg.evaluate(_CARD_LINES)


def read_job(pg: Page) -> dict:
    return pg.evaluate(_EXTRACT)


def discover(ctx: BrowserContext, searches: list[dict], known: Callable[[str], bool],
             max_pages: int = 40, title_ok: Callable[[str], bool] = lambda t: True,
             max_age_days: int = 2, log=print) -> Iterator[Posting]:
    pg = browser.page(ctx)
    if not browser.is_logged_in(ctx, "naukri"):
        log("  naukri: not signed in; run `jobright login naukri`")
        return
    opened = 0
    for s in searches:
        pg.goto(search_url(s["keywords"], s.get("location", "india")), wait_until="domcontentloaded")
        _pause(pg, 4, 6)
        raw = _load_cards(pg)
        picks = []
        for idx, lines in enumerate(raw):
            c = parse_card(lines)
            if not c or c["age"] > max_age_days or not title_ok(c["title"]):
                continue
            if store.seen(c["company"], c["title"]):
                continue
            picks.append((idx, c))
        log(f"  naukri '{s['keywords']}' / {s.get('location')}: {len(raw)} cards, {len(picks)} recent with a target title")
        for idx, c in picks:
            if opened >= max_pages:
                return
            try:
                with ctx.expect_page(timeout=15000) as new:
                    pg.locator(_CARDS).nth(idx).click()
                tab = new.value
                tab.wait_for_load_state("domcontentloaded")
                _pause(tab, 3, 5)
            except Exception as e:
                log(f"  naukri: could not open {c['title']} @ {c['company']}: {e}")
                continue
            opened += 1
            url = tab.url.split("?")[0]
            m = re.search(r"-(\d{9,})$", url)
            d = read_job(tab)
            tab.close()
            if not m or known(m.group(1)) or d["applied"] or not d["desc"]:
                continue
            desc = d["header"] + "\n\n" + d["desc"]
            if d["skills"]:
                desc += "\n\nKey skills: " + ", ".join(d["skills"])
            yield Posting(
                source="naukri",
                external_id=m.group(1),
                url=url,
                apply_url=url,
                apply_type="external" if d["companySite"] or c["company_site"] else "naukri",
                # The card's fields are cleanly separated; the job page's are not.
                title=c["title"] or d["title"],
                company=c["company"] or d["company"],
                location=c["location"] or d["location"],
                description=desc.strip(),
            )
            _pause(pg)
