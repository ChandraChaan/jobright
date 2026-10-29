"""From an interpreted field to the exact string to type or option to pick.

resolve()  finds the fact: answers.yaml, the master profile, saved answers,
           or the job itself (budget, source). Personal facts come only from
           you; nothing here estimates, rounds up or fills a gap.
render()   turns that fact into candidates in the field's type and unit
           ("8.7" years → "8" for a whole-number field; 30 LPA → "3000000"
           for a rupee field; +91 phone → national digits when the form
           has its own country-code box), best first.
validate() checks a candidate against the field's own constraints before
           it is typed, and pick() maps a value onto a dropdown's options.
recover()  reads the site's validation message and chooses the candidate
           that satisfies it, so a form error leads to a corrected answer
           instead of a stuck application.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from jobright import salary
from jobright.apply.fields import FieldSpec, Intent, Interpretation, norm, wants_integer, wants_number

_UNSET = {None, "", "FILL_ME"}
_MONTHS = "January February March April May June July August September October November December".split()


@dataclass
class JobContext:
    job_id: str | None = None
    source: str | None = None            # linkedin | naukri | greenhouse | lever | ashby
    company: str = ""
    title: str = ""
    budget_max_lpa: float | None = None
    expected_ctc_override: float | None = None
    siblings: list[str] = field(default_factory=list)   # other labels in the same form step
    flags: list[str] = field(default_factory=list)      # e.g. salary review, set while answering


@dataclass
class Fact:
    value: object                        # str | float | bool | list[str]
    source: str                          # where it came from, for the audit trail
    unit: str | None = None              # unit of `value`: years lpa days


def unset(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in _UNSET)


def _yes_no(v) -> str | None:
    if unset(v):
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    s = str(v).strip().lower()
    if s in ("yes", "y", "true"):
        return "Yes"
    if s in ("no", "n", "false"):
        return "No"
    return None


# --- the profile, as words ---------------------------------------------------

@lru_cache(maxsize=1)
def _profile():
    from jobright.profile import load_profile
    return load_profile()


@lru_cache(maxsize=1)
def _profile_words() -> str:
    from jobright.pipeline import profile_text
    return " " + norm(profile_text(_profile())) + " "


_SKILL_NOISE = re.compile(r"\b(development|developer|developers|domain|technology|technologies|programming|"
                          r"engineering|platforms?|frameworks?|framework|apps?|applications?|based|native|"
                          r"skills?|knowledge|tools?|stack|sdk|projects?)\b")
_SKILL_ALIASES = {"ios": "ios", "iphone": "ios", "android": "android", "flutter": "flutter", "dart": "dart",
                  "mobile": "mobile", "cross platform": "cross platform", "react native": "react native",
                  "node": "node.js", "nodejs": "node.js", "node.js": "node.js", "aws": "aws", "kotlin": "kotlin",
                  "swift": "swift", "java": "java", "firebase": "firebase"}


def _skill_key(s: str) -> str:
    s = _SKILL_NOISE.sub(" ", norm(s))
    s = re.sub(r"\s+", " ", s).strip()
    return _SKILL_ALIASES.get(s, s)


def _skill_items(subject: str) -> list[str]:
    raw = re.split(r",|/|\band\b|\bor\b|&|\+", subject)
    items = [_skill_key(x) for x in raw if _skill_key(x)]
    if len(items) == 1 and " " in items[0] and items[0] not in _profile_words():
        # "ios android flutter" (commas lost): check word by word
        words = [w for w in items[0].split() if len(w) > 1]
        if all(f" {w} " in _profile_words() for w in words):
            return words
    return items


def in_profile(skill: str) -> bool:
    k = _skill_key(skill)
    if not k:
        return False
    pw = _profile_words()
    if f" {k} " in pw:
        return True
    from jobright.coverage import keyword_coverage
    have, _ = keyword_coverage([k], pw)
    return bool(have)


# --- resolve -----------------------------------------------------------------

def _years_for(subject: str, bank: dict) -> Fact | None:
    rel = bank.get("relevant_experience_years") or {}
    want = _skill_items(subject)
    found = []
    for item in want:
        hit = None
        for name, years in rel.items():
            key = _skill_key(name)
            if key and (key == item or re.search(rf"\b{re.escape(key)}\b", item) or re.search(rf"\b{re.escape(item)}\b", key)):
                hit = (name, years)
                break
        if hit is None or unset(hit[1]):
            return None
        found.append(hit)
    if not found:
        return None
    # Several skills in one question: the smallest is the one that is true of all of them.
    name, years = min(found, key=lambda x: float(x[1]))
    return Fact(float(years), f"answers.yaml: relevant_experience_years.{name}", "years")


_RATE = re.compile(r"\b(hourly|per hour|an hour|hour rate|daily rate|day rate|per day|a day)\b|/\s*(hr|hour|day)\b")


def salary_facts(bank: dict) -> tuple[str, float | None, float | None]:
    """(unit, current, expected) from the answer bank. unit is "lpa" (India) or
    "annual"; older banks used current_ctc_lpa / expected_ctc_lpa."""
    unit = str(bank.get("salary_unit") or ("lpa" if "current_ctc_lpa" in bank or "expected_ctc_lpa" in bank else "annual"))
    def num(*keys):
        for k in keys:
            v = bank.get(k)
            if not unset(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None
    return unit, num("current_salary", "current_ctc_lpa"), num("expected_salary", "expected_ctc_lpa")


_COUNTRIES = {
    "IN": r"india|indian", "GB": r"uk|u k|united kingdom|britain|great britain|england|scotland|wales",
    "US": r"us|usa|u s|united states|america", "CA": r"canada", "DE": r"germany", "IE": r"ireland",
    "NL": r"netherlands", "AU": r"australia", "SG": r"singapore", "AE": r"uae|dubai|emirates", "EU": r"europe|eu",
}


def _countries_named(text: str) -> set[str]:
    return {code for code, rx in _COUNTRIES.items() if re.search(rf"\b({rx})\b", text)}


def _home_country() -> str:
    from jobright import config
    return (config.settings().get("profile") or {}).get("country") or "IN"


def resolve(interp: Interpretation, spec: FieldSpec, ctx: JobContext, bank: dict, saved: dict) -> Fact | None:
    i, p = interp.intent, _profile()
    c = p.contact
    first, *rest = c.name.split()
    links = {x.label.lower(): x.url for x in c.links}
    role = p.roles[0]
    b = lambda key: None if unset(bank.get(key)) else bank.get(key)

    if i == Intent.FIRST_NAME:
        return Fact(first, "profile: contact.name")
    if i == Intent.LAST_NAME:
        return Fact(" ".join(rest), "profile: contact.name")
    if i == Intent.MIDDLE_NAME:
        return None
    if i == Intent.FULL_NAME:
        return Fact(c.name, "profile: contact.name")
    if i == Intent.EMAIL:
        return Fact(c.email, "profile: contact.email")
    if i == Intent.PHONE:
        return Fact(c.phone, "profile: contact.phone")
    if i == Intent.PHONE_COUNTRY:
        return Fact("India (+91)" if c.phone.startswith("+91") else c.phone.split()[0], "profile: contact.phone")
    if i == Intent.LINKEDIN:
        return Fact(links.get("linkedin"), "profile: links") if links.get("linkedin") else None
    if i == Intent.GITHUB:
        return Fact(links.get("github"), "profile: links") if links.get("github") else None
    if i == Intent.PORTFOLIO:
        return Fact(links.get("portfolio"), "profile: links") if links.get("portfolio") else None
    if i == Intent.CITY:
        return Fact(b("current_location") or c.location.split(",")[0], "answers.yaml: current_location")
    if i == Intent.COUNTRY:
        return Fact(c.location.split(",")[-1].strip(), "profile: contact.location")
    if i == Intent.PREFERRED_LOCATION:
        locs = b("preferred_locations")
        return Fact(locs, "answers.yaml: preferred_locations") if locs else None
    if i in (Intent.CURRENT_COMPANY, Intent.EMPLOYER_NAME):
        return Fact(role.company, "profile: roles[0].company")
    if i in (Intent.CURRENT_TITLE, Intent.JOB_TITLE):
        return Fact(role.title, "profile: roles[0].title")
    if i == Intent.START_MONTH:
        return Fact(_MONTHS[int(role.start.split("-")[1]) - 1], "profile: roles[0].start")
    if i == Intent.START_YEAR:
        return Fact(role.start.split("-")[0], "profile: roles[0].start")
    if i in (Intent.END_MONTH, Intent.END_YEAR):
        return None  # the current job has no end date; the form's "current role" box covers it
    if i == Intent.CURRENT_ROLE:
        return Fact(True, "profile: roles[0] is current")

    if i == Intent.TOTAL_EXPERIENCE:
        v = b("total_experience_years")
        return Fact(float(v), "answers.yaml: total_experience_years", "years") if v is not None else None
    if i == Intent.SKILL_YEARS:
        return _years_for(interp.subject or "", bank) if interp.subject else None
    if i == Intent.HAS_SKILL:
        items = _skill_items(interp.subject or "")
        if items and all(in_profile(x) for x in items):
            return Fact("Yes", f"profile: shows {', '.join(items)}")
        return None  # not shown in the profile: you decide, once

    if i in (Intent.CURRENT_CTC, Intent.EXPECTED_CTC):
        if _RATE.search(norm(spec.text)):
            return None  # your salary is yearly; an hourly or day rate is yours to give, once
        unit, cur, exp = salary_facts(bank)
        if i == Intent.CURRENT_CTC:
            return Fact(cur, f"answers.yaml: current_salary ({unit})", unit) if cur is not None else None
        if exp is None:
            return None
        d = salary.expected_ctc(ctx.budget_max_lpa, cur if cur is not None else 0.0, exp,
                                ctx.expected_ctc_override, style="lpa" if unit == "lpa" else "annual")
        if d.expected is None:
            ctx.flags.append(f"salary review: {d.reason}")
            return None
        return Fact(float(d.expected), f"salary rule: {d.reason}", unit)
    if i == Intent.NOTICE_PERIOD:
        v = b("notice_period_days")
        return Fact(float(v), "answers.yaml: notice_period_days", "days") if v is not None else None
    if i == Intent.SERVING_NOTICE:
        v = _yes_no(bank.get("currently_serving_notice"))
        return Fact(v, "answers.yaml: currently_serving_notice") if v else None
    if i == Intent.LAST_WORKING_DAY:
        v = b("last_working_day")
        return Fact(str(v), "answers.yaml: last_working_day") if v else None

    if i == Intent.PASSPORT and re.search(r"\btravel", norm(spec.text)):
        # "valid passport AND willingness to travel": true only if both are
        pp, tr = _yes_no(bank.get("has_passport")), _yes_no(bank.get("willing_to_travel"))
        if pp is None or tr is None:
            return None
        return Fact("Yes" if pp == tr == "Yes" else "No", "answers.yaml: has_passport + willing_to_travel")
    for intent, key in ((Intent.RELOCATE, "willing_to_relocate"), (Intent.REMOTE, "open_to_remote"),
                        (Intent.HYBRID, "open_to_hybrid"), (Intent.ONSITE, "open_to_onsite"),
                        (Intent.PASSPORT, "has_passport"), (Intent.TRAVEL, "willing_to_travel")):
        if i == intent:
            v = _yes_no(bank.get(key))
            return Fact(v, f"answers.yaml: {key}") if v else None

    if i in (Intent.SPONSORSHIP, Intent.WORK_AUTH):
        auth = str(bank.get("work_authorization") or bank.get("work_authorization_india") or "").strip()
        home = _home_country()
        named = _countries_named(norm(spec.text))
        abroad = bool(named) and home not in named
        if i == Intent.SPONSORSHIP:
            if abroad:
                v = _yes_no(bank.get("needs_visa_sponsorship_abroad", bank.get("needs_visa_sponsorship")))
                return Fact(v, "answers.yaml: needs_visa_sponsorship_abroad") if v else None
            v = _yes_no(bank.get("needs_sponsorship"))
            if v:
                return Fact(v, "answers.yaml: needs_sponsorship")
            if re.search(r"citizen|settled|indefinite leave|ilr|permanent resident|green card|no sponsorship", auth, re.I):
                return Fact("No", f"answers.yaml: work_authorization ({auth})")
            return None
        if abroad or not auth or unset(auth):
            return None
        if spec.yes_no or spec.kind in ("radio", "checkbox", "checkbox_group"):
            return Fact("Yes", f"answers.yaml: work_authorization ({auth})")
        return Fact(auth, "answers.yaml: work_authorization")

    if i == Intent.HAS_DEGREE:
        deg = norm(" ".join(e.degree for e in p.education))
        want = norm(interp.subject or "")
        bachelor_rx = r"\b(bachelor\w*|b tech|b\.tech|btech|be|b e|beng|b eng|bsc|b sc|ba|bca|bba|bcom|graduat\w*|undergraduate)\b"
        master_rx = r"\b(master\w*|m tech|mtech|msc|m sc|ma|meng|mba|mca|postgraduate)\b"
        has_bachelor = bool(re.search(bachelor_rx, deg)) or bool(re.search(master_rx, deg))
        has_master = bool(re.search(master_rx, deg)) or bool(re.search(r"\b(phd|doctor)", deg))
        if re.search(r"phd|doctor", want):
            return Fact("Yes" if re.search(r"\b(phd|doctor)", deg) else "No", "profile: education")
        if re.search(master_rx, want):
            return Fact("Yes" if has_master else "No", "profile: education")
        if re.search(bachelor_rx, want):
            return Fact("Yes" if has_bachelor else "No", "profile: education")
        return None
    if i == Intent.DEGREE_GRADE:
        v = b("degree_grade") or next((e.grade for e in p.education if getattr(e, "grade", None)), None)
        return Fact(str(v), "answers.yaml: degree_grade") if v else None
    if i == Intent.DRIVING:
        v = _yes_no(bank.get("driving_licence"))
        return Fact(v, "answers.yaml: driving_licence") if v else None
    if i == Intent.EDUCATION:
        return Fact(b("highest_education") or p.education[0].degree, "answers.yaml: highest_education")
    if i == Intent.INSTITUTION:
        return Fact(p.education[0].institution, "profile: education")
    if i == Intent.GRADUATION_YEAR:
        v = b("graduation_year")
        return Fact(str(v), "answers.yaml: graduation_year") if v else None
    if i == Intent.GENDER:
        v = b("gender")
        return Fact(str(v), "answers.yaml: gender") if v else None
    if i == Intent.DOB:
        v = b("date_of_birth")
        return Fact(str(v), "answers.yaml: date_of_birth") if v else None
    if i == Intent.ENGLISH:
        v = b("english_proficiency")
        return Fact(str(v), "answers.yaml: english_proficiency") if v else None
    if i == Intent.HEARD_ABOUT:
        src = {"linkedin": "LinkedIn", "naukri": "Naukri", "reed": "Reed", "indeed": "Indeed"}.get(ctx.source or "", "Company website")
        return Fact(src, f"job source: {ctx.source}")
    if i == Intent.CONSENT:
        from jobright import config
        ok = config.settings()["apply"].get("accept_application_consent", False)
        return Fact(True, "settings: apply.accept_application_consent") if ok else None
    return None


# --- render ------------------------------------------------------------------

def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}".rstrip("0").rstrip(".")


def _phone_parts(phone: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+") and len(digits) > 10:
        return digits[:-10], digits[-10:]
    return "", digits[-10:]


def render(fact: Fact, spec: FieldSpec, interp: Interpretation, ctx: JobContext) -> list[str]:
    """Candidate strings in the field's type and unit, best first."""
    v, i = fact.value, interp.intent
    if isinstance(v, bool):
        return ["Yes" if v else "No"]
    if isinstance(v, list):
        return [", ".join(map(str, v))] + [str(x) for x in v]

    if i == Intent.PHONE:
        cc, national = _phone_parts(str(v))
        has_cc_box = any(re.search(r"country code|phone country|dialing code|isd", norm(s)) for s in ctx.siblings)
        short = has_cc_box or (spec.maxlength and spec.maxlength <= 10) or (spec.pattern and "10" in spec.pattern)
        full = f"+{cc} {national}" if cc else national
        return [national, full] if short else [full, national, f"+{cc}{national}"]

    if fact.unit == "years":
        years = float(v)
        months = round(years * 12)
        if interp.unit == "months":
            return [str(months)]
        whole = str(math.floor(years))  # never round experience up
        if wants_integer(spec):
            return [whole]
        if years.is_integer():
            return [whole]
        return [_num(years), whole] if spec.kind == "number" or wants_number(spec, interp) else [f"{_num(years)} years", _num(years), whole]

    if fact.unit == "lpa":
        lpa = float(v)
        rupees = str(int(round(lpa * 1e5)))
        monthly = str(int(round(lpa * 1e5 / 12)))
        u = interp.unit
        hint = norm(spec.hint)
        if u == "inr_month":
            return [monthly]
        if u == "inr" or re.search(r"\b\d{6,}\b", hint):
            return [rupees, _num(lpa)]
        if wants_number(spec, interp):
            return [_num(lpa), rupees]
        return [f"{_num(lpa)} LPA", _num(lpa)]

    if fact.unit == "annual":
        amount = float(v)
        u, hint = interp.unit, norm(spec.hint)
        if u in ("inr_month", "monthly"):
            return [str(int(round(amount / 12)))]
        if u == "thousands" or re.search(r"\bk\b|in thousands|000s", norm(spec.text)):
            return [_num(round(amount / 1000, 1)), str(int(amount))]
        if wants_number(spec, interp):
            return [str(int(round(amount))), _num(round(amount / 1000, 1))]
        return [f"{amount:,.0f}", str(int(round(amount)))]

    if fact.unit == "days":
        days = float(v)
        u = interp.unit
        if u == "months":
            return [_num(round(days / 30 * 2) / 2 or 0)]
        if u == "weeks":
            return [str(math.ceil(days / 7))]
        if spec.kind in ("number",) or wants_number(spec, interp) or spec.inputmode == "numeric":
            return [_num(days)]
        return ["Immediately" if days == 0 else f"{_num(days)} days", _num(days)]

    if i == Intent.DOB and spec.kind == "date":
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return [datetime.strptime(str(v), fmt).strftime("%Y-%m-%d")]
            except ValueError:
                pass
    s = str(v)
    if spec.maxlength and len(s) > spec.maxlength:
        s = s[:spec.maxlength]
    return [s]


# --- choose among options ----------------------------------------------------

_SYNONYMS = {
    "yes": ["yes", "y", "true", "i am", "i do", "i have", "available"],
    "no": ["no", "n", "false", "i am not", "i do not", "not available"],
    "male": ["male", "man", "he/him"],
    "female": ["female", "woman", "she/her"],
}


def pick(options: list[str], value: str) -> str | None:
    if not options or value is None:
        return None
    v = str(value).strip().lower()
    low = [o.strip().lower() for o in options]
    tests = [
        lambda ol: ol == v,
        lambda ol: ol.startswith(v) or v.startswith(ol),
        lambda ol: re.search(rf"\b{re.escape(v)}\b", ol) is not None,
        lambda ol: len(v) > 2 and v in ol,
        # the option is a whole word inside your answer: "Full professional" → "Professional"
        lambda ol: len(ol) > 3 and re.search(rf"\b{re.escape(ol)}\b", v) is not None,
    ]
    for test in tests:
        for o, ol in zip(options, low):
            if test(ol):
                return o
    for key, alts in _SYNONYMS.items():
        if v == key or v in alts:
            for o, ol in zip(options, low):
                if any(ol == a or ol.startswith(a + " ") or ol.startswith(a + "'") or ol.startswith(a + ",") for a in alts):
                    return o
    num = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*(?:days?|months?|years?|yrs?|lpa|lakhs?))?", v)
    if num:  # a number into a range option: "14 days" → "15 days or less", "8" → "7-10 years"
        n = float(num.group(1))
        for o in options:
            m = re.search(r"(\d+(?:\.\d+)?)\s*\w*\s*(or less|or below|and below|or under|or fewer)", o, re.I)
            if m and n <= float(m.group(1)):
                return o
        for o in options:
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
            if len(nums) == 2 and nums[0] <= n <= nums[1]:
                return o
        for o in options:
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
            if len(nums) == 1 and re.search(r"\+|more|above|over", o, re.I) and n >= nums[0]:
                return o
        if n == 0:
            for o in options:
                if re.search(r"immediate", o, re.I):
                    return o
    return None


# --- validate & recover -------------------------------------------------------

def validate(value: str, spec: FieldSpec, interp: Interpretation) -> str | None:
    if spec.kind in ("select", "radio", "checkbox_group") and spec.options:
        return None if value in spec.options else "not one of the options"
    if spec.maxlength and len(value) > spec.maxlength:
        return f"longer than {spec.maxlength} characters"
    numeric = wants_number(spec, interp)
    if numeric:
        if not re.fullmatch(r"\d+(\.\d+)?", value):
            return "not a number"
        x = float(value)
        if spec.min is not None and x < spec.min:
            return f"below minimum {spec.min:g}"
        if spec.max is not None and x > spec.max:
            return f"above maximum {spec.max:g}"
        if wants_integer(spec) and not x.is_integer():
            return "not a whole number"
    if spec.pattern:
        try:
            if not re.fullmatch(spec.pattern, value):
                return f"does not match pattern {spec.pattern}"
        except re.error:
            pass
    if interp.intent == Intent.PHONE and len(re.sub(r"\D", "", value)) < 10:
        return "phone number too short"
    return None


def _limits_from_error(error: str) -> tuple[float | None, float | None]:
    e = error.lower()
    m = re.search(r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)", e)
    if m:
        return float(m.group(1)), float(m.group(2))
    lo = re.search(r"(?:larger|greater|more|bigger) than\s+(-?\d+(?:\.\d+)?)", e)
    hi = re.search(r"(?:less|smaller|lower) than\s+(-?\d+(?:\.\d+)?)", e)
    return (float(lo.group(1)) if lo else None), (float(hi.group(1)) if hi else None)


def recover(candidates: list[str], tried: list[str], error: str, spec: FieldSpec,
            interp: Interpretation) -> str | None:
    """The next candidate that satisfies the site's error message, or None."""
    e = error.lower()
    spec2 = FieldSpec(**{**spec.__dict__, "error": error})
    lo, hi = _limits_from_error(e)
    pool = list(candidates)
    if re.search(r"whole number|integer", e):
        pool = [str(math.floor(float(c))) for c in pool if re.fullmatch(r"\d+(\.\d+)?", c)] + pool
    if re.search(r"phone|mobile|10 digit|digits", e):
        pool = [re.sub(r"\D", "", c)[-10:] for c in pool] + pool
    for c in dict.fromkeys(pool):
        if c in tried:
            continue
        if validate(c, spec2, interp):
            continue
        if (lo is not None or hi is not None) and re.fullmatch(r"\d+(\.\d+)?", c):
            x = float(c)
            if (lo is not None and x <= lo and "larger" in e) or (lo is not None and x < lo) or (hi is not None and x > hi):
                continue
        return c
    return None
