"""Naukri apply. "Quick apply" submits your Naukri profile at once; some jobs
then ask recruiter questions in a chat drawer, answered here one by one with
the same field understanding as forms.

Naukri sends the resume on your Naukri profile, not a per-job file, so keep
that profile resume current (see `jobright general`). Success is read from
the job page itself ("Applied"), because after Quick apply Naukri moves on to
a "Similar opportunities" page.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from jobright.answers import answer_field
from jobright.apply import Outcome, guard
from jobright.apply.fields import FieldSpec
from jobright.apply.forms import screenshot
from jobright.apply.values import JobContext, pick
from jobright.sources.naukri import read_job

_DONE = re.compile(r"successfully applied|applied successfully|application (has been )?sent|you have applied", re.I)


def _drawer(page: Page):
    return page.locator("[class*='chatbot_Drawer'], [class*='chatbot-drawer'], [class*='chatbot_DrawerContentWrapper'], "
                        "[role=dialog]:has(textarea), [role=dialog]:has([contenteditable=true])").first


def _answer_chat(page: Page, ctx: JobContext, audit: list[dict]) -> list[str]:
    """Answer chat questions until the drawer closes. Returns unanswered questions."""
    asked: dict[str, int] = {}
    for _ in range(20):
        drawer = _drawer(page)
        if not drawer.count() or not drawer.is_visible():
            return []
        bot = drawer.locator("[class*='botMsg'], [class*='bot-msg'], li[class*='bot']")
        question = bot.last.inner_text().strip() if bot.count() else ""
        if not question:
            page.wait_for_timeout(1000)
            continue
        asked[question] = asked.get(question, 0) + 1
        if asked[question] > 2:   # the same question again: our answer was not accepted
            return [f"{question} (Naukri did not accept the answer)"]
        chips = drawer.locator("[class*='chatbot_Chip'], [class*='ssrc__radio-btn-container'] label, [class*='chip']")
        options = [c.strip() for c in chips.all_inner_texts() if c.strip()]
        box = drawer.locator("div[contenteditable='true'], textarea, input[type=text], input[type=number]").last
        hint = (box.get_attribute("placeholder") or "") if box.count() else ""
        spec = FieldSpec(n="chat", kind="radio" if options else "text", label=question, hint=hint,
                         options=options, required=True)
        res = answer_field(spec, ctx)
        if res is None:
            return [question]
        audit.append({"field": question, "value": res.value, "source": res.source, "intent": res.interp.intent.value})
        if options:
            choice = pick(options, res.value)
            if not choice:
                return [question]
            chips.nth(options.index(choice)).click()
        else:
            box.click()
            if box.evaluate("e => e.tagName") == "DIV":  # contenteditable chat input
                page.keyboard.type(res.value)
            else:
                box.fill(res.value)
        send = drawer.locator("[class*='sendMsg'], [class*='send'], button:has-text('Save')").last
        if send.count():
            send.click()
        page.wait_for_timeout(1800)
    return ["chat questions did not finish"]


def _apply_button(page: Page):
    """The button whose label in view is "Quick apply" / "Apply". TopTier keeps the
    hidden "Applied" label in the same button, so its accessible name has both."""
    found = page.evaluate(r"""() => {
      for (const b of document.querySelectorAll('button')) {
        const br = b.getBoundingClientRect();
        if (!br.width) continue;
        const own = el => [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
        const shown = [b, ...b.querySelectorAll('span, div')].filter(k => own(k))
          .filter(k => { const r = k.getBoundingClientRect(); return r.width && r.top >= br.top - 1 && r.bottom <= br.bottom + 1; })
          .map(k => own(k).toLowerCase());
        const label = shown.join(' ');
        if (label === 'quick apply' || label === 'apply') { b.setAttribute('data-jr-apply', '1'); return true; }
      }
      return false;
    }""")
    if found:
        return page.locator("button[data-jr-apply='1']").last
    b = page.locator("#apply-button")
    return b.first if b.count() else None


def _open(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)


def apply(page: Page, job: dict, resume_pdf: Path, dry_run: bool = False,
          ctx: JobContext | None = None, log=lambda m: None) -> Outcome:
    ctx = ctx or JobContext(job_id=job["id"], source="naukri")
    notes: list[str] = []
    audit: list[dict] = []
    _open(page, job["url"])
    issue = guard.page_issue(page)
    if issue:
        return Outcome("unavailable" if issue.kind == "unavailable" else "blocked", issue.detail,
                       screenshot=screenshot(page, job["id"], issue.kind))
    notes += guard.clear(page, log=log)
    state = read_job(page)
    if state["applied"]:
        return Outcome("applied", "already applied on Naukri", evidence="Naukri shows Applied")
    if not state["desc"] and not state["title"]:
        _open(page, job["url"])   # a second look before calling it gone
        state = read_job(page)
        if not state["desc"] and not state["title"]:
            return Outcome("unavailable", "the job page is empty (posting removed)")
    if state["companySite"] and not state["quick"]:
        return Outcome("needs_you", "this Naukri job applies on the company site",
                       screenshot=screenshot(page, job["id"], "company_site"))
    btn = _apply_button(page)
    if not btn:
        return Outcome("needs_you", "no Quick apply button",
                       screenshot=screenshot(page, job["id"], "no_apply"), notes=notes)
    if dry_run:
        return Outcome("dry_run", "Quick apply button found; Naukri submits on click, so stopped here",
                       screenshot=screenshot(page, job["id"], "before_apply"), notes=notes)
    btn.click()
    page.wait_for_timeout(3500)
    notes += guard.clear(page, keep=_drawer(page) if _drawer(page).count() else None, log=log)
    unanswered = _answer_chat(page, ctx, audit)
    shot = screenshot(page, job["id"], "after_apply")
    if unanswered:
        return Outcome("needs_you", "Naukri asked questions only you can answer; open the job to finish",
                       unanswered, shot, audit=audit, notes=notes)
    page.wait_for_timeout(1500)
    m = _DONE.search(page.inner_text("body")[:8000])
    if m:
        return Outcome("applied", "Naukri confirmed", screenshot=shot, evidence=m.group(0), audit=audit, notes=notes)
    _open(page, job["url"])
    if read_job(page)["applied"]:
        return Outcome("applied", "Naukri shows Applied", screenshot=screenshot(page, job["id"], "applied"),
                       evidence="Naukri shows Applied", audit=audit, notes=notes)
    return Outcome("unverified", "Quick apply pressed; Naukri showed no confirmation yet", screenshot=shot,
                   audit=audit, notes=notes)
