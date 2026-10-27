"""Page health and interruptions.

page_issue()   says when a page is not something to work on: the posting
               was removed or closed, a security check / CAPTCHA appeared,
               or the session was signed out.
clear()        closes popups that sit on top of the work: settings and
               profile prompts, "save this job?", promos, cookie banners,
               chat widgets. It only ever presses a closing control (Close,
               Dismiss, Not now, No thanks, Skip, Cancel, ×) or the
               privacy-preserving cookie choice, then Escape as a last
               resort; it never presses Accept, Submit, Apply, Update or
               Upgrade. The dialog being worked on is left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Locator, Page

UNAVAILABLE = re.compile(
    r"job id provided may not be valid|job posting has been removed|no longer accepting applications|"
    r"this job is no longer available|job (has )?expired|this job has expired|posting (has )?(been )?(closed|removed)|"
    r"position (has been )?(filled|closed)|job (is )?(closed|no longer active)|we couldn.?t find (this|that) job|"
    r"job not found|page not found|this page doesn.?t exist|the job you are looking for|requisition (is )?(closed|no longer)|"
    r"no longer open|applications are closed|job is not available",
    re.I,
)
SECURITY = re.compile(
    r"verify you are (a )?human|are you a robot|unusual activity|security (check|verification)|"
    r"let.?s do a quick security check|complete the captcha|prove you.?re not a robot",
    re.I,
)
_CLOSE = re.compile(r"^\s*(close|dismiss|not now|no,? thanks|no thank you|skip( for now)?|maybe later|later|"
                    r"remind me later|cancel|got it|ok(ay)?|continue( browsing| without)?|×|✕|x)\s*$", re.I)
_CLOSE_LABEL = re.compile(r"\b(close|dismiss)\b", re.I)
_COOKIE_SAFE = re.compile(r"^\s*(reject( all)?( cookies)?|decline( all)?|only (necessary|essential)( cookies)?|"
                          r"necessary only|essential only|deny|use necessary cookies only)\s*$", re.I)
_NEVER = re.compile(r"accept|agree|allow|submit|apply|update|upgrade|buy|subscribe|premium|delete|remove|"
                    r"confirm|share|send|follow|save job|try", re.I)


@dataclass
class Issue:
    kind: str      # unavailable | security | logged_out
    detail: str


def page_issue(page: Page, site: str | None = None) -> Issue | None:
    url = page.url or ""
    if re.search(r"/checkpoint/|/authwall|captcha|challenge", url):
        return Issue("security", f"security check at {url[:80]}")
    if re.search(r"linkedin\.com/(login|uas/login)|naukri\.com/nlogin", url):
        return Issue("logged_out", "the site signed the bot out")
    for frame in page.frames:
        fu = frame.url or ""
        if ("recaptcha" in fu and "bframe" in fu) or ("hcaptcha.com" in fu and "challenge" in fu):
            try:
                if frame.frame_element().is_visible():
                    return Issue("security", "CAPTCHA shown")
            except Exception:
                pass
    try:
        text = page.inner_text("body", timeout=5000)[:30000]
    except Exception:
        return None
    if SECURITY.search(text):
        return Issue("security", SECURITY.search(text).group(0))
    m = UNAVAILABLE.search(text)
    if m:
        return Issue("unavailable", m.group(0))
    return None


# Overlays that block the page: dialogs, alert dialogs, and large fixed layers.
_FIND = r"""
(keepSel) => {
  const keep = keepSel ? document.querySelector(keepSel) : null;
  const vw = innerWidth, vh = innerHeight;
  const out = [];
  let n = 0;
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 30 && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none'; };
  const cands = new Set([...document.querySelectorAll('dialog[open], [role=dialog], [role=alertdialog], [aria-modal=true], #onetrust-banner-sdk, [id*=cookie i], [class*=cookie i]')]);
  // Large fixed layers count only when they cover the middle of the screen
  // (a modal or a full-page promo), not headers, sidebars or chat docks.
  for (const e of document.querySelectorAll('body *')) {
    const s = getComputedStyle(e);
    if (s.position === 'fixed' && (parseInt(s.zIndex) || 0) >= 100) {
      const r = e.getBoundingClientRect();
      const coversCenter = r.left < vw / 2 && r.right > vw / 2 && r.top < vh / 2 && r.bottom > vh / 2;
      if (coversCenter && r.width * r.height > vw * vh * 0.15 && e.querySelector('button, [role=button]')) cands.add(e);
    }
  }
  const items = [...cands].filter(e => visible(e)
      && !(keep && (e === keep || e.contains(keep) || keep.contains(e)))
      && !(e.closest('header, nav') && !e.matches('[role=dialog], dialog')));
  // Report the innermost overlay of a nested group once.
  for (const e of items) {
    if (items.some(o => o !== e && e.contains(o))) continue;
    e.setAttribute('data-jr-ov', String(n));
    out.push({n, text: (e.innerText || '').trim().slice(0, 300),
              dialog: e.matches('dialog, [role=dialog], [role=alertdialog], [aria-modal=true]')});
    n++;
  }
  return out;
}
"""


def _safe_close(overlay: Locator) -> str | None:
    """Press the safest closing control inside one overlay. Returns what was pressed."""
    buttons = overlay.locator("button, [role=button], a[role=button], a[aria-label]")
    texts = []
    for i in range(min(buttons.count(), 25)):
        b = buttons.nth(i)
        try:
            if not b.is_visible():
                continue
            t = (b.inner_text(timeout=1000) or "").strip()
            a = b.get_attribute("aria-label") or ""
        except Exception:
            continue
        texts.append((b, t, a))
    for b, t, a in texts:                       # cookie banners: the privacy-preserving choice
        if _COOKIE_SAFE.match(t):
            b.click()
            return t
    for b, t, a in texts:                       # an explicit close control
        if (_CLOSE.match(t) or (not t and _CLOSE_LABEL.search(a))) and not _NEVER.search(t):
            b.click()
            return t or a
    return None


def clear(page: Page, keep: Locator | None = None, log=None, max_rounds: int = 4) -> list[str]:
    """Close interrupting overlays; returns a note per overlay closed."""
    closed: list[str] = []
    keep_sel = None
    if keep is not None:
        try:
            if keep.count():
                keep.evaluate("e => e.setAttribute('data-jr-keep', '1')")
                keep_sel = "[data-jr-keep='1']"
        except Exception:
            keep_sel = None
    for _ in range(max_rounds):
        try:
            found = page.evaluate(_FIND, keep_sel)
        except Exception:
            return closed
        if not found:
            break
        progressed = False
        for ov in found:
            loc = page.locator(f"[data-jr-ov='{ov['n']}']")
            pressed = _safe_close(loc)
            if pressed is None and ov.get("dialog") and keep_sel is None:
                # Escape closes most modals; with a kept dialog open it would close that one too.
                page.keyboard.press("Escape")
                pressed = "Escape"
            if pressed:
                note = f"closed popup ({pressed}): {ov['text'][:80]!r}"
                closed.append(note)
                if log:
                    log("    " + note)
                progressed = True
                page.wait_for_timeout(700)
        if not progressed:
            break
    return closed


def install(page: Page) -> None:
    """Browser-level interruptions: JS alert/confirm boxes and leave-page prompts."""
    def on_dialog(d):
        try:
            d.accept() if d.type == "beforeunload" else d.dismiss()
        except Exception:
            pass
    page.on("dialog", on_dialog)
