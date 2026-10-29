"""What a form field is asking for.

A FieldSpec is everything the page tells us about one field: its label, help
text, placeholder, input type, constraints (maxlength, min/max, step,
pattern) and any validation error on screen. `classify` turns that into an
Interpretation: the intent (total experience, phone, expected CTC…), the
subject (which skill), and the unit the answer must be in (years, LPA,
rupees, days, months).

Rules come first and are ordered so specific intents win over generic ones:
experience and salary questions are recognised before contact fields, so
"years of mobile app development experience" can never be read as "mobile
(phone) number". Labels on real forms carry typos ("expereince",
"Developement"), so key words are matched with a similarity cutoff, not
exact spelling.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    OTHER_FILE = "other_file"
    CONSENT = "consent"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    MIDDLE_NAME = "middle_name"
    FULL_NAME = "full_name"
    EMAIL = "email"
    PHONE = "phone"
    PHONE_COUNTRY = "phone_country"
    COUNTRY = "country"
    LINKEDIN = "linkedin"
    GITHUB = "github"
    PORTFOLIO = "portfolio"
    CITY = "current_location"
    PREFERRED_LOCATION = "preferred_location"
    RELOCATE = "relocate"
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    CURRENT_COMPANY = "current_company"
    CURRENT_TITLE = "current_title"
    EMPLOYER_NAME = "employment_company"
    JOB_TITLE = "employment_title"
    START_MONTH = "employment_start_month"
    START_YEAR = "employment_start_year"
    END_MONTH = "employment_end_month"
    END_YEAR = "employment_end_year"
    CURRENT_ROLE = "employment_current"
    TOTAL_EXPERIENCE = "total_experience"
    SKILL_YEARS = "skill_years"
    HAS_SKILL = "has_skill"
    CURRENT_CTC = "current_ctc"
    EXPECTED_CTC = "expected_ctc"
    NOTICE_PERIOD = "notice_period"
    SERVING_NOTICE = "serving_notice"
    LAST_WORKING_DAY = "last_working_day"
    HAS_DEGREE = "has_degree"
    EDUCATION = "education"
    INSTITUTION = "institution"
    GRADUATION_YEAR = "graduation_year"
    DEGREE_GRADE = "degree_grade"
    DRIVING = "driving_licence"
    SPONSORSHIP = "sponsorship"
    WORK_AUTH = "work_authorization"
    GENDER = "gender"
    EEO = "eeo"
    DOB = "date_of_birth"
    HEARD_ABOUT = "heard_about"
    ENGLISH = "english_proficiency"
    PASSPORT = "passport"
    TRAVEL = "travel"
    MOTIVATION = "motivation"
    UNKNOWN = "unknown"


# Answers to these come only from you: never from the profile or a model.
PERSONAL = {Intent.CURRENT_CTC, Intent.NOTICE_PERIOD, Intent.SERVING_NOTICE, Intent.LAST_WORKING_DAY,
            Intent.RELOCATE, Intent.REMOTE, Intent.HYBRID, Intent.ONSITE, Intent.SPONSORSHIP,
            Intent.WORK_AUTH, Intent.GENDER, Intent.EEO, Intent.DOB, Intent.GRADUATION_YEAR,
            Intent.ENGLISH, Intent.PASSPORT, Intent.TRAVEL, Intent.MOTIVATION, Intent.SKILL_YEARS,
            Intent.DEGREE_GRADE, Intent.DRIVING,
            Intent.TOTAL_EXPERIENCE, Intent.PREFERRED_LOCATION}


@dataclass
class FieldSpec:
    n: str                      # data-jr handle on the page
    kind: str                   # text number tel email url textarea select radio checkbox checkbox_group combobox file date
    label: str
    hint: str = ""              # help text, placeholder, aria-description
    name: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    value: str = ""
    maxlength: int | None = None
    min: float | None = None
    max: float | None = None
    step: str | None = None
    pattern: str | None = None
    inputmode: str | None = None
    error: str = ""

    @property
    def text(self) -> str:
        return f"{self.label} {self.hint}".strip()

    @property
    def yes_no(self) -> bool:
        opts = {o.strip().lower() for o in self.options}
        return bool(opts) and opts <= {"yes", "no", "y", "n", "true", "false"}


@dataclass
class Interpretation:
    intent: Intent
    subject: str | None = None
    unit: str | None = None     # years months days weeks lpa inr inr_month
    confidence: float = 1.0
    source: str = "rule"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#/.&]+", " ", (s or "").lower())).strip()


_VOCAB = ["experience", "development", "developer", "years", "notice", "expected", "current", "salary",
          "relocate", "passport", "proficiency", "graduation", "qualification", "sponsorship", "authorized",
          "compensation", "willing", "immediately", "available"]


def _fix_typos(text: str) -> str:
    """Replace near-misses of key words ('expereince' → 'experience')."""
    out = []
    for w in norm(text).split():
        if len(w) > 5 and w not in _VOCAB:
            m = difflib.get_close_matches(w, _VOCAB, n=1, cutoff=0.84)
            w = m[0] if m else w
        out.append(w)
    return " ".join(out)


def _any(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


_EXP = r"\b(experience|exp|yrs?|years?|yoe)\b"
_NOT_EXP = r"\b(graduat\w*|passing|passed out|start date|end date|date|birth|from year|to year|year of)\b"
_GENERIC_SUBJECT = re.compile(
    r"^(total|overall|relevant|professional|work|working|industry|it|software|software development|"
    r"full time|post qualification|your|you|of|in|the|career|job)?$")


_SUBJECT_PATTERNS = [
    # "... experience in Flutter", "years have you worked with Kotlin"
    re.compile(r"\b(?:experience|exp|years?|yrs?)\b.*?\b(?:in|with|of|on|using|for|as)\s+(?:a |an |the )?(.+?)\s*(?:\?|$|\()"),
    # "Mobile App Development experience (in years)"
    re.compile(r"^(?:how many )?(?:years? of )?(?:your )?(?:total |relevant )?(.+?)\s+(?:experience|exp)\b"),
]
_FILLER = re.compile(
    r"\b(do you have|you have|have you|did you|years?|yrs?|experience|exp|of|relevant|total|hands on|hands-on|"
    r"professional|work|worked|working|in|with|on|your|the|a|an|how many|please|mention|enter|number of|"
    r"overall|do|you|have|what is|what s|is|are|been|gained|got|strong|good)\b")


_UNIT_WORDS = re.compile(r"\(?\b(?:in|no of|number of)\s+(?:years?|yrs?|months?|days?)\b\)?|\((?:years?|yrs?|months?)\)")


def _subject(t: str) -> str | None:
    """The skill an experience question is about, or None for total experience."""
    t = _UNIT_WORDS.sub(" ", t)
    for pattern in _SUBJECT_PATTERNS:
        m = pattern.search(t)
        if not m:
            continue
        s = re.sub(r"\s+", " ", _FILLER.sub(" ", m.group(1).strip(" ?."))).strip()
        if s and not _GENERIC_SUBJECT.match(s):
            return s
    return None


def _unit_of(t: str, intent: Intent) -> str | None:
    if intent in (Intent.CURRENT_CTC, Intent.EXPECTED_CTC):
        if _any(t, r"\b(per month|monthly|p ?m)\b"):
            return "inr_month"
        if _any(t, r"\bin thousands\b|\b000s\b|\(k\)"):
            return "thousands"
        if _any(t, r"\b(lpa|lakhs?|lacs?|lac|l p a|in l)\b"):
            return "lpa"
        if _any(t, r"\b(inr|rupees|rs)\b|₹|\bin (absolute|numbers?|figures)\b|\bannual (ctc|salary) in\b"):
            return "inr"
        if _any(t, r"£|\$|€|\b(gbp|usd|eur|per annum|annual|a year|per year)\b"):
            return "annual"
        return None
    if intent in (Intent.NOTICE_PERIOD,):
        if _any(t, r"\bmonths?\b"):
            return "months"
        if _any(t, r"\bweeks?\b"):
            return "weeks"
        return "days"
    if intent in (Intent.TOTAL_EXPERIENCE, Intent.SKILL_YEARS):
        if _any(t, r"\bin months\b|\(months\)|\bno of months\b"):
            return "months"
        return "months" if _any(t, r"\bmonths\b") and not _any(t, r"\byears?\b") else "years"
    return None


def classify(spec: FieldSpec, siblings: list[str] | None = None) -> Interpretation:
    t = _fix_typos(spec.text)
    lab = _fix_typos(spec.label)
    kind = spec.kind

    def it(intent: Intent, subject: str | None = None, conf: float = 1.0) -> Interpretation:
        return Interpretation(intent, subject, _unit_of(t, intent), conf)

    if kind == "file":
        if _any(t + " " + spec.name.lower(), r"\b(resume|cv|curriculum)\b"):
            return it(Intent.RESUME)
        if _any(t, r"\bcover\b"):
            return it(Intent.COVER_LETTER)
        return it(Intent.OTHER_FILE, conf=0.5)

    if kind == "checkbox" and _any(t, r"\b(consent|agree|privacy|terms|policy|acknowledge|gdpr|data processing|i confirm|declare|certify)\b"):
        return it(Intent.CONSENT)
    if kind == "checkbox" and _any(t, r"\b(current(ly)? (role|job|position|employer)|i currently work here|present)\b"):
        return it(Intent.CURRENT_ROLE)

    # Salary and notice before anything that could share their words.
    ctc = r"\b(ctc|salary|compensation|package|pay|remuneration|fixed)\b"
    if _any(t, r"\b(expected|desired|expectation|asking)\b") and _any(t, ctc):
        return it(Intent.EXPECTED_CTC)
    if _any(t, r"\b(current|present|existing|last drawn|latest)\b") and _any(t, ctc):
        return it(Intent.CURRENT_CTC)
    if _any(t, r"\bserving\b.*\bnotice\b|\bon notice\b|\bcurrently in notice\b"):
        return it(Intent.SERVING_NOTICE)
    if _any(t, r"\blast working (day|date)\b|\blwd\b"):
        return it(Intent.LAST_WORKING_DAY)
    if _any(t, r"\bnotice\b|how soon .*(join|start)|when can you (join|start)|joining (time|date|period)|start date availability|earliest (start|joining)|\bjoin immediately\b|\bavailable to (join|start)\b"):
        return it(Intent.NOTICE_PERIOD)

    # Experience, before contact fields ("mobile app development experience" is not a phone number).
    if _any(t, _EXP) and not _any(lab, _NOT_EXP) and not _any(t, r"\bgraduation year\b"):
        subject = _subject(lab)   # the label names the skill; help text only describes the format
        yes_no_q = spec.yes_no or (kind in ("radio", "checkbox_group", "select") and not spec.options) \
            or _any(lab, r"^(do|does|have|are|is|did|can) you\b")
        if yes_no_q and subject and not _any(lab, r"\bhow (many|much|long)\b"):
            return it(Intent.HAS_SKILL, subject)
        return it(Intent.SKILL_YEARS, subject) if subject else it(Intent.TOTAL_EXPERIENCE)
    work_mode = r"\b(hybrid|remote|office|on ?site|relocat\w*|travel\w*|shifts?|night|weekends?|commut\w*|wfh|wfo)\b"
    if _any(lab, r"^(do|have|are) you (have )?(hands on |working |good |strong )?(knowledge|experience|exposure|worked|used|familiar|proficient|comfortable|skilled)\b") \
            and not _any(t, work_mode):
        m = re.search(r"\b(?:with|in|on|of|using)\s+(.+?)\s*\??$", lab)
        return it(Intent.HAS_SKILL, m.group(1) if m else None, 0.8)

    if _any(t, r"\bcountry code\b|\bphone country\b|\bdialing code\b|\bisd\b"):
        return it(Intent.PHONE_COUNTRY)
    phone = _any(t, r"\b(phone|telephone|cell ?phone|whatsapp)\b", r"\bmobile\s*(phone|number|no)\b",
                 r"\bcontact\s*(number|no)\b") or lab in ("mobile", "mobile number", "phone")
    if phone and not _any(t, r"\b(develop\w*|app|apps|application|engineer\w*|platform|experience|years)\b"):
        return it(Intent.PHONE)
    if kind == "email" or _any(t, r"\be ?-?mail\b"):
        return it(Intent.EMAIL)
    if _any(lab, r"\bfirst name\b|\bgiven name\b|\bforename\b"):
        return it(Intent.FIRST_NAME)
    if _any(lab, r"\bmiddle name\b"):
        return it(Intent.MIDDLE_NAME)
    if _any(lab, r"\blast name\b|\bsurname\b|\bfamily name\b"):
        return it(Intent.LAST_NAME)
    if _any(lab, r"^(your |full |legal |candidate )?name$|\bfull name\b|\byour name\b|\bcandidate name\b"):
        return it(Intent.FULL_NAME)
    if _any(t, r"\blinked ?in\b"):
        return it(Intent.LINKEDIN)
    if _any(t, r"\bgit ?hub\b"):
        return it(Intent.GITHUB)
    if _any(t, r"\b(portfolio|website|personal (site|url|page)|blog)\b") or kind == "url":
        return it(Intent.PORTFOLIO, conf=0.9)

    if _any(t, r"\brelocat\w*\b"):
        return it(Intent.RELOCATE)
    if _any(t, r"\bpreferred (work )?location|\blocation preference\b"):
        return it(Intent.PREFERRED_LOCATION)
    if _any(t, r"\b(contract\w*|freelanc\w*|part[ -]time|temporary|hourly|c2c|c2h)\b"):
        return it(Intent.UNKNOWN)   # the kind of engagement, not just where: yours to answer, once
    if _any(t, r"\b(work from office|wfo|on ?site|in office|in-office|commut\w*|office \d+ days)\b"):
        return it(Intent.ONSITE)
    if _any(t, r"\bhybrid\b"):
        return it(Intent.HYBRID)
    if _any(t, r"\bremote(ly)?\b|\bwork from home\b|\bwfh\b"):
        return it(Intent.REMOTE)
    if _any(lab, r"^(current |present )?(city|location)$|\bcurrent (city|location)\b|\bwhere are you (located|based)\b|\bcity of residence\b"):
        return it(Intent.CITY)
    if _any(lab, r"^country( of residence)?$|\bcountry of residence\b"):
        return it(Intent.COUNTRY)

    if _any(t, r"\bsponsor\w*\b|\bvisa\b"):
        return it(Intent.SPONSORSHIP)
    if _any(t, r"\b(authori[sz]ed|eligible|legally|right) to work\b|\bwork (permit|authori[sz]ation)\b"):
        return it(Intent.WORK_AUTH)
    if _any(t, r"\bpassport\b"):
        return it(Intent.PASSPORT)
    if _any(t, r"\btravel\w*\b"):
        return it(Intent.TRAVEL)
    if _any(t, r"\benglish\b|\blanguage proficiency\b"):
        return it(Intent.ENGLISH)

    if _any(lab, r"\bcurrent (company|employer|organi[sz]ation)\b|\bcurrently working (at|for|with)\b"):
        return it(Intent.CURRENT_COMPANY)
    if _any(lab, r"\bcurrent (title|designation|role|position|job title)\b"):
        return it(Intent.CURRENT_TITLE)
    if _any(lab, r"^(company|employer|organi[sz]ation)( name)?$"):
        return it(Intent.EMPLOYER_NAME)
    if _any(lab, r"^(job )?title$|^designation$|^position$"):
        return it(Intent.JOB_TITLE)
    if _any(lab, r"^(start date|from) month$"):
        return it(Intent.START_MONTH)
    if _any(lab, r"^(start date|from) year$"):
        return it(Intent.START_YEAR)
    if _any(lab, r"^(end date|to) month$"):
        return it(Intent.END_MONTH)
    if _any(lab, r"^(end date|to) year$"):
        return it(Intent.END_YEAR)

    if _any(t, r"\bdriving licen[cs]e\b|\bdriver.?s licen[cs]e\b|\bfull uk licen[cs]e\b"):
        return it(Intent.DRIVING)
    if _any(t, r"\b(degree (classification|grade|result)|classification|cgpa|gpa|percentage|grade (achieved|obtained)|2 ?1|2 ?2|first class)\b"):
        return it(Intent.DEGREE_GRADE)
    if _any(t, r"\b(graduat\w*|passing|passed out)\b") and _any(t, r"\byear\b"):
        return it(Intent.GRADUATION_YEAR)
    if _any(t, r"\b(completed|have|hold|possess)\b.*\b(bachelor|master|degree|b ?tech|b ?e|graduate|diploma|phd)\b"):
        m = re.search(r"\b(bachelor\w*|master\w*|b ?tech|b ?e|m ?tech|phd|diploma|graduat\w*)", t)
        return it(Intent.HAS_DEGREE, m.group(1) if m else None)
    if _any(lab, r"\b(university|college|institut\w*|school)\b"):
        return it(Intent.INSTITUTION)
    if _any(lab, r"\b(highest )?(education|qualification|degree)\b"):
        return it(Intent.EDUCATION)

    if _any(t, r"\bgender\b|\bpronoun"):
        return it(Intent.GENDER)
    if _any(t, r"\b(race|ethnicity|veteran|disabilit\w*|lgbt\w*)\b"):
        return it(Intent.EEO)
    if _any(t, r"\b(date of birth|dob|birth ?date)\b"):
        return it(Intent.DOB)
    if _any(t, r"\bhow did you (hear|find|come)\b|\bsource of (application|hire)\b|\breferr?al source\b"):
        return it(Intent.HEARD_ABOUT)
    if _any(t, r"\bwhy (do you|are you|would you|should we)\b|\bwhat (interests|excites|motivates)\b|\bcover letter\b|\btell us about yourself\b"):
        return it(Intent.MOTIVATION)
    return Interpretation(Intent.UNKNOWN, confidence=0.0)


# --- data type --------------------------------------------------------------

def wants_number(spec: FieldSpec, interp: Interpretation) -> bool:
    if spec.kind == "number" or (spec.inputmode or "") in ("numeric", "decimal"):
        return True
    if spec.pattern and re.fullmatch(r"\^?(\\d|\[0-9\])[+*{].*", spec.pattern):
        return True
    if re.search(r"\b(whole|decimal) number\b|\bnumeric\b|\bnumber only\b|\bdigits only\b", (spec.hint + " " + spec.error).lower()):
        return True
    return interp.intent in (Intent.TOTAL_EXPERIENCE, Intent.SKILL_YEARS, Intent.CURRENT_CTC, Intent.EXPECTED_CTC) \
        and spec.kind in ("text", "number", "tel")


def wants_integer(spec: FieldSpec) -> bool:
    if spec.step in ("1",) or re.search(r"\bwhole number\b|\binteger\b|\bwithout decimals?\b", (spec.hint + " " + spec.error).lower()):
        return True
    if spec.pattern and re.fullmatch(r"\^?(\\d|\[0-9\])(\+|\*|\{\d+(,\d*)?\})\$?", spec.pattern):
        return True
    return False
