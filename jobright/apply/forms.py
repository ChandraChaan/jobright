"""Read a form, answer every field, recover from the site's validation errors.

scan() describes each visible field as a FieldSpec, including help text,
placeholder, constraints and any error shown for it. fill() answers each one
through answers.answer_field (which validates before anything is typed).

Pre-filled values: sites such as LinkedIn pre-fill answers saved from older
applications, and those can be wrong (a phone number in an experience box).
Contact fields keep the site's value unless it is flagged invalid; answer
fields (experience, CTC, notice, yes/no questions) are set to the value your
data gives whenever it differs. A field showing an error is always redone,
using the error text to pick a format the site accepts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Locator, Page

from jobright import config
from jobright.answers import Resolution, answer_field, interpret
from jobright.apply.fields import PERSONAL, FieldSpec, Intent, classify
from jobright.apply.values import JobContext, pick, recover

_pick = pick  # older engines import this name

_SCAN = r"""
(root) => {
  const out = []; let n = 0;
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const byIds = ids => (ids || '').split(/\s+/).map(i => document.getElementById(i)).filter(Boolean);
  const isErr = e => /error|invalid|feedback--error|alert/i.test((e.id || '') + ' ' + (e.className || '') + ' ' + (e.getAttribute('role') || ''));
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = byIds(lb).map(x => x.innerText).join(' '); if (clean(t)) return clean(t); }
    const al = el.getAttribute('aria-label');
    if (al) return clean(al);
    const wrap = el.closest('label');
    if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = p.querySelector('label, legend, [class*="label"], [class*="question"], [class*="title"]');
      if (l && l !== el && !l.contains(el) && clean(l.innerText)) return clean(l.innerText);
    }
    return clean(el.getAttribute('placeholder') || el.name || '');
  };
  const box = el => {   // the field's own block: label + input + help + error
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.querySelectorAll('input, select, textarea').length > 3) break;
      if (p.querySelector('label, legend')) return p;
    }
    return el.parentElement || el;
  };
  const errorOf = el => {
    const msgs = [];
    for (const d of byIds(el.getAttribute('aria-describedby') || el.getAttribute('aria-errormessage'))) if (isErr(d) && clean(d.innerText)) msgs.push(clean(d.innerText));
    const b = box(el);
    for (const d of b.querySelectorAll('[class*="error" i], [role=alert], .artdeco-inline-feedback--error, [data-test-form-element-error-messages]'))
      if (clean(d.innerText) && !d.contains(el)) msgs.push(clean(d.innerText));
    return [...new Set(msgs)].join(' ').slice(0, 300);
  };
  const hintOf = el => {
    const parts = [el.getAttribute('placeholder') || '', el.getAttribute('title') || '', el.getAttribute('aria-description') || ''];
    for (const d of byIds(el.getAttribute('aria-describedby'))) if (!isErr(d)) parts.push(d.innerText || '');
    const b = box(el);
    for (const d of b.querySelectorAll('[class*="hint" i], [class*="help" i], [class*="description" i], small')) if (!d.contains(el) && !isErr(d)) parts.push(d.innerText || '');
    return clean(parts.join(' ')).slice(0, 300);
  };
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const num = v => (v === null || v === '' || isNaN(+v)) ? null : +v;
  const groups = new Set();
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName;
    let type = tag === 'SELECT' ? 'select' : tag === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (type !== 'file' && !visible(el)) continue;
    if (el.getAttribute('aria-hidden') === 'true') continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' || (type === 'checkbox' && el.name && root.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const key = type + ':' + (el.name || el.id);
      if (groups.has(key)) continue;
      groups.add(key);
      const items = el.name ? [...root.querySelectorAll(`input[type=${type}][name="${CSS.escape(el.name)}"]`)] : [el];
      const fs = el.closest('fieldset');
      let raw = fs && fs.querySelector('legend') ? fs.querySelector('legend').innerText : '';
      if (!clean(raw)) {
        let p = el.closest('[role="radiogroup"], [role="group"]') || el.parentElement?.parentElement;
        for (let i = 0; i < 3 && p && !clean(raw); i++, p = p.parentElement) {
          const l = p.querySelector('legend, label:not(:has(input)), [class*="label"], [class*="question"], span[class*="title"]');
          if (l && !items.some(r => l.contains(r))) raw = l.innerText;
        }
      }
      const q = clean(raw), starred = /\*\s*$/.test((raw || '').trim());
      items.forEach((r, i) => r.setAttribute('data-jr', `${n}-${i}`));
      const err = fs ? [...fs.querySelectorAll('[class*="error" i], [role=alert]')].map(d => clean(d.innerText)).filter(Boolean).join(' ') : errorOf(el);
      out.push({n: String(n), kind: type === 'radio' ? 'radio' : 'checkbox_group', label: q, hint: '', name: el.name || '',
                options: items.map(labelFor), required: items.some(r => r.required) || !!fs?.querySelector('[aria-required="true"]') || required || starred,
                value: (items.find(r => r.checked) && labelFor(items.find(r => r.checked))) || '', error: err});
      n++; continue;
    }
    if (el.getAttribute('role') === 'combobox' && type !== 'search') type = 'combobox';
    el.setAttribute('data-jr', String(n));
    let value = type === 'checkbox' ? (el.checked ? 'on' : '') : (el.value || '');
    if (type === 'select') { const o = el.options[el.selectedIndex]; value = o && el.value ? clean(o.text) : ''; }
    if (type === 'combobox') { const shown = el.closest('[class*="control"], [class*="select"]')?.innerText || ''; value = /^\s*select/i.test(shown) ? '' : (el.value || clean(shown)); }
    out.push({n: String(n), kind: type, label: labelFor(el), hint: hintOf(el), name: el.name || el.id || '', required,
              value, error: errorOf(el),
              options: type === 'select' ? [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--|please select)/i.test(t)) : [],
              maxlength: el.maxLength > 0 ? el.maxLength : null, min: num(el.getAttribute('min')), max: num(el.getAttribute('max')),
              step: el.getAttribute('step'), pattern: el.getAttribute('pattern'), inputmode: el.getAttribute('inputmode')});
    n++;
  }
  return out;
}
"""

# Contact details: keep what the site pre-filled unless it is flagged invalid.
_KEEP_PREFILL = {Intent.EMAIL, Intent.PHONE, Intent.PHONE_COUNTRY, Intent.FIRST_NAME, Intent.LAST_NAME,
                 Intent.FULL_NAME, Intent.MIDDLE_NAME, Intent.CITY, Intent.COUNTRY}


@dataclass
class FillResult:
    filled: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)   # label, value, source: for the job's history
    flags: list[str] = field(default_factory=list)


def scan(root: Locator) -> list[FieldSpec]:
    return [FieldSpec(**f) for f in root.evaluate(_SCAN)]


def _options(page: Page) -> list[str]:
    return [o.strip() for o in page.locator("[role=option]:visible").all_inner_texts() if o.strip()]


def _set(page: Page, root: Locator, spec: FieldSpec, value: str) -> None:
    loc = root.locator(f'[data-jr="{spec.n}"]')
    if spec.kind in ("radio", "checkbox_group"):
        i = spec.options.index(value)
        root.locator(f'[data-jr="{spec.n}-{i}"]').check(force=True)
    elif spec.kind == "select":
        loc.select_option(label=value)
    elif spec.kind == "checkbox":
        loc.check(force=True) if value.lower().startswith(("y", "on", "true")) else loc.uncheck(force=True)
    elif spec.kind == "combobox":
        loc.click()
        page.wait_for_timeout(400)
        loc.fill(value)
        page.wait_for_timeout(700)
        opts = _options(page)
        choice = pick(opts, value) if opts else None
        if choice:
            page.locator("[role=option]:visible").filter(has_text=choice).first.click()
        else:
            page.keyboard.press("Escape")
            raise ValueError(f"no option matching {value!r}")
    else:
        loc.fill("")
        loc.fill(value)
        loc.evaluate("e => e.dispatchEvent(new Event('blur', {bubbles: true}))")


def _combobox_resolution(page: Page, root: Locator, spec: FieldSpec, ctx: JobContext) -> Resolution | None:
    """Comboboxes show their options only once opened."""
    loc = root.locator(f'[data-jr="{spec.n}"]')
    loc.click()
    page.wait_for_timeout(500)
    opts = _options(page)
    page.keyboard.press("Escape")
    if 0 < len(opts) <= 80:
        spec.options = opts
    return answer_field(spec, ctx, record_missing=spec.required)


def fill(page: Page, root: Locator | None, resume_pdf: Path, ctx: JobContext | str | None = None) -> FillResult:
    """Answer every field under `root` (default: the whole page)."""
    root = root or page.locator("body")
    if not isinstance(ctx, JobContext):
        ctx = JobContext(job_id=ctx)
    specs = scan(root)
    ctx.siblings = [s.label for s in specs]
    result = FillResult()

    # Mark the current job first, so employment end dates are not required.
    current_role = False
    for s in specs:
        if s.kind == "checkbox" and re.search(r"\b(current(ly)? (role|job|position|employer)|i currently work here)\b", s.label, re.I):
            if not s.value:
                root.locator(f'[data-jr="{s.n}"]').check(force=True)
            current_role = True
            result.filled.append((s.label, "checked"))

    for s in specs:
        label = s.label or s.name
        if not s.required and re.search(r"\b(is|are) required\b|\brequired field\b", s.error or "", re.I):
            s.required = True     # marked only by its error, e.g. LinkedIn's "This field is required"
        try:
            if current_role and re.search(r"^(end date|to) (month|year)$", label, re.I):
                continue
            if s.kind == "file":
                if re.search(r"\b(resume|cv|curriculum)\b", f"{label} {s.name}", re.I) or (s.required and not re.search(r"cover", label, re.I)):
                    root.locator(f'[data-jr="{s.n}"]').set_input_files(str(resume_pdf))
                    result.filled.append((label or "resume", resume_pdf.name))
                elif s.required:
                    result.missing.append(label or "file upload")
                continue
            if s.kind == "checkbox" and re.search(r"\bfollow\b", label, re.I):
                if s.value:   # "Follow <company>": opt out
                    root.locator(f'[data-jr="{s.n}"]').uncheck(force=True)
                continue
            if s.kind == "checkbox" and re.search(r"current(ly)? (role|job|position)|currently work here", label, re.I):
                continue
            ruled = classify(s, ctx.siblings)
            if s.value and not s.error and ruled.intent in _KEEP_PREFILL:
                continue          # the site's own contact details, e.g. your LinkedIn account email
            if not s.required and ruled.intent == Intent.UNKNOWN:
                continue          # optional and unfamiliar: leave it rather than spend a lookup on it

            res = _combobox_resolution(page, root, s, ctx) if s.kind == "combobox" else \
                answer_field(s, ctx, record_missing=s.required)
            if res is None:
                if s.required and not s.value:
                    result.missing.append(label)
                elif s.required and s.error:
                    result.missing.append(f"{label} (site says: {s.error})")
                elif s.required and s.value and interpret(s, ctx).intent in PERSONAL:
                    # A personal answer the site remembered from an earlier form, which your
                    # answer bank cannot confirm: ask once rather than pass it on unseen.
                    result.missing.append(f"{label} (site pre-filled {s.value!r}; confirm it once)")
                continue

            current = (s.value or "").strip()
            if current and not s.error:
                if res.interp.intent in _KEEP_PREFILL or current.lower() == res.value.lower():
                    continue
                result.corrected.append(f"{label}: {current!r} → {res.value!r}")
            value = res.value
            if s.error and current:
                # The site rejected what is there: choose a format its message allows.
                value = recover(res.candidates, [current], s.error, s, res.interp) or value
                if value == current:
                    result.missing.append(f"{label} (site says: {s.error})")
                    continue
            _set(page, root, s, value)
            result.filled.append((label, value))
            result.audit.append({"field": label, "value": value, "source": res.source,
                                 "intent": res.interp.intent.value})
        except Exception as e:  # one odd widget must not sink the form
            if s.required:
                result.missing.append(f"{label} (could not fill: {type(e).__name__})")
    result.missing = list(dict.fromkeys(result.missing))
    result.flags = list(dict.fromkeys(ctx.flags))
    return result


def field_errors(root: Locator) -> list[str]:
    """Validation messages currently shown under `root`."""
    try:
        return [e for e in dict.fromkeys(t.strip() for t in root.locator(
            "[class*=error i]:visible, [role=alert]:visible, .artdeco-inline-feedback--error:visible").all_inner_texts()) if e]
    except Exception:
        return []


def screenshot(page: Page, job_id: str, tag: str) -> str:
    out = config.DATA / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{re.sub(r'[^A-Za-z0-9]+', '_', job_id)}_{tag}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        return ""
    return str(path)


def has_captcha(page: Page) -> bool:
    from jobright.apply.guard import page_issue
    issue = page_issue(page)
    return bool(issue and issue.kind == "security")
