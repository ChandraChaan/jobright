"""Free mode: everything the AI does, done with rules and your own facts.

analyze()      reads a JD without a model: required vs preferred skills from
               the JD's own sections and a skills vocabulary, years asked,
               seniority, and a fit score from skill coverage, track match,
               level and years.
tailor()       builds a resume from your facts word for word, ranked by how
               much of the JD each fact covers (recent roles weigh more).
               Nothing is reworded, so nothing can be invented.
parse_resume() a rule-based CV parser: contact, sections, dated roles with
               bullets, education, skills, projects.
plan_career()  level, strengths and role tracks from your dated roles and skills.

Less polished than the AI engines, but $0, private, and honest by construction.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from jobright import config
from jobright.coverage import keyword_coverage

# A broad skills vocabulary, so JD skills are found even when you do not have them.
VOCAB = """
Python, Java, JavaScript, TypeScript, Kotlin, Swift, Dart, Go, Golang, Rust, C++, C#, Ruby, PHP, Scala, SQL, NoSQL, Bash,
Objective-C, MATLAB, Elixir, Haskell, Solidity, HTML, CSS, SASS, Tailwind,
React, React Native, Next.js, Vue, Angular, Svelte, Redux, Node.js, Express, NestJS, Django, Flask, FastAPI, Spring, Spring Boot,
.NET, ASP.NET, Ruby on Rails, Laravel, CodeIgniter, Flutter, Jetpack Compose, SwiftUI, UIKit, Xamarin, Ionic, Electron,
GraphQL, REST APIs, gRPC, WebSockets, Microservices, Serverless, Kafka, RabbitMQ, Redis, Elasticsearch,
PostgreSQL, MySQL, MongoDB, DynamoDB, Cassandra, SQLite, Oracle, Snowflake, BigQuery, Redshift, Spark, Hadoop, Airflow, dbt,
Pandas, NumPy, scikit-learn, TensorFlow, PyTorch, Keras, Machine Learning, Deep Learning, NLP, Computer Vision, LLM, LLMs,
Generative AI, GenAI, RAG, LangChain, LlamaIndex, OpenAI, Prompt Engineering, Vector Databases, Hugging Face, MLOps,
Data Engineering, Data Analysis, ETL, Power BI, Tableau, Excel, Statistics,
AWS, Azure, GCP, Google Cloud, Firebase, Cloud Firestore, Supabase, Heroku, Vercel, Docker, Kubernetes, Terraform, Ansible,
Helm, Linux, CI/CD, Jenkins, GitHub Actions, GitLab CI, CircleCI, Codemagic, Fastlane, Git, Jira, Confluence, Agile, Scrum,
Kanban, TDD, BDD, Unit Testing, Integration Testing, Widget Testing, End-to-end Testing, Jest, Pytest, JUnit, Selenium,
Cypress, Playwright, Appium, Android, iOS, Mobile Development, Cross-Platform Development, Bluetooth Low Energy, BLE,
Push Notifications, In-App Purchases, Clean Architecture, MVVM, MVC, BLoC, Provider, Riverpod, GetX, MobX, State Management,
Design Patterns, SOLID, System Design, Distributed Systems, Scalability, Performance Optimization, Application Security,
OAuth, JWT, SSL Pinning, Encryption, Payment Gateways, Stripe, Razorpay, Accessibility, Figma, UI/UX, Responsive Design,
SEO, Technical Leadership, Team Leadership, Mentoring, Code Reviews, Stakeholder Management, Product Management,
Observability, Monitoring, Prometheus, Grafana, Datadog, Sentry, Crashlytics, New Relic
"""
_VOCAB = sorted({w.strip() for w in VOCAB.replace("\n", " ").split(",") if w.strip()}, key=len, reverse=True)

_PREFERRED = re.compile(r"(nice to have|nice-to-have|preferred|bonus|plus|desirable|good to have|would be great|ideally)", re.I)
_REQUIRED = re.compile(r"(requirements|required|must have|must-have|qualifications|what you.?ll need|what we.?re looking for|"
                       r"skills|essential|you have|about you|you bring|experience with)", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|architect|head|manager|director|vp)\b", re.I)
_JUNIOR = re.compile(r"\b(intern|internship|trainee|graduate|junior|jr\.?|entry.level|associate|apprentice|fresher)\b", re.I)


# Skill names that are also everyday words: they count only when written as the skill is.
_CASED = {"Go", "Swift", "Spring", "Express", "Provider", "Excel", "Oracle", "Rust", "Ionic", "Helm", "Spark",
          "Agile", "Scrum", "Kanban", "Git", "Jest", "Vue", "React", "Redux", "Flutter", "Dart", "Ruby", "Electron"}


def find_skills(text: str, extra: list[str] = ()) -> list[str]:
    """Skills named in `text`: from the vocabulary and the user's own skill list."""
    found, low = [], " " + re.sub(r"\s+", " ", text.lower()) + " "
    plain = " " + re.sub(r"\s+", " ", text) + " "
    for term in list(dict.fromkeys(list(extra) + _VOCAB)):
        t = term.lower()
        if len(t) < 2:
            continue
        hay, needle = (plain, term) if term in _CASED else (low, t)
        pat = rf"(?<![A-Za-z0-9+#]){re.escape(needle)}(?![A-Za-z0-9+#])"
        if re.search(pat, hay) and not any(t in f.lower() and t != f.lower() for f in found):
            found.append(term)
    return found


def _sections(jd: str) -> tuple[str, str]:
    """(required-ish text, preferred-ish text) by the JD's headings."""
    req, pref, mode = [], [], "req"
    for line in jd.splitlines():
        stripped = line.strip()
        heading = len(stripped) < 60 and (stripped.endswith(":") or stripped.isupper() or not re.search(r"[.]$", stripped))
        inline = stripped.split(":", 1)[1].strip() if ":" in stripped else ""   # "Nice to have: Kotlin, Swift"
        if _PREFERRED.search(stripped) and (heading or len(stripped) < 120):
            mode = "pref"
            if not heading or inline:
                pref.append(inline or stripped)
            continue
        if heading and _REQUIRED.search(stripped):
            mode = "req"
            if inline:
                req.append(inline)
            continue
        (pref if mode == "pref" else req).append(stripped)
    return "\n".join(req), "\n".join(pref)


def _years_total(profile) -> float:
    b = {}
    try:
        from jobright.answers import bank
        b = bank()
    except Exception:
        pass
    v = b.get("total_experience_years")
    if isinstance(v, (int, float)):
        return float(v)
    return role_years(profile)


def role_years(profile) -> float:
    months = 0
    today = date.today()
    for r in profile.roles:
        if r.engagement and re.search(r"intern|placement|teaching assistant|volunteer", r.engagement, re.I):
            continue
        if re.search(r"\bintern\b|teaching assistant", r.title, re.I):
            continue
        try:
            y0, m0 = map(int, r.start.split("-")[:2])
            y1, m1 = (map(int, r.end.split("-")[:2]) if r.end else (today.year, today.month))
            months += max(0, (y1 - y0) * 12 + (m1 - m0))
        except Exception:
            continue
    return round(months / 12, 1)


def _level(years: float, profile) -> str:
    titles = " ".join([*profile.titles, *(r.title for r in profile.roles[:2])])
    if years < 1:
        return "graduate"
    if re.search(r"\b(principal|staff|architect|director|head)\b", titles, re.I):
        return "principal"
    if re.search(r"\b(lead|manager)\b", titles, re.I) and years >= 5:
        return "lead"
    return "junior" if years < 2 else "mid" if years < 5 else "senior"


def analyze(profile, jd_text: str, title: str = "", company: str = "", location: str = ""):
    """A JobAnalysis without a model."""
    from jobright import salary
    from jobright.jd import JobAnalysis, years_band
    from jobright.pipeline import profile_text

    mine = [s for items in profile.skills.values() for s in items]
    req_text, pref_text = _sections(jd_text)
    required = find_skills(req_text, mine)
    preferred = [s for s in find_skills(pref_text, mine) if s not in required]
    if not required and not preferred:
        required = find_skills(jd_text, mine)
    ptext = profile_text(profile)
    have_r, miss_r = keyword_coverage(required, ptext)
    have_p, _ = keyword_coverage(preferred, ptext)
    total = 2 * len(required) + len(preferred)
    skill_pct = round(100 * (2 * len(have_r) + len(have_p)) / total) if total else 40

    years = _years_total(profile)
    level = _level(years, profile)
    lo, hi = years_band(jd_text)
    t = title or (jd_text.splitlines()[0] if jd_text else "")
    senior_title, junior_title = bool(_SENIOR.search(t)), bool(_JUNIOR.search(t))
    fit = skill_pct
    notes = [f"{len(have_r)}/{len(required)} required skills"]
    from jobright.runner import track_of
    track = track_of(t)
    if track:
        fit += 10
        notes.append(f"title fits your {track} track")
    if level in ("graduate", "junior") and senior_title and not junior_title:
        fit -= 30
        notes.append("senior role for an early-career profile")
    if level in ("senior", "lead", "principal") and junior_title and not senior_title:
        fit -= 25
        notes.append("junior role for a senior profile")
    if lo is not None and lo > years + 1:
        fit -= min(45, 15 + 5 * round(lo - years))          # 2 years short: -25; 6+ short: -45
        notes.append(f"asks {lo:g}+ years, you have {years:g}")
    if hi is not None and hi < years - 4:
        fit -= 10
    fit = max(0, min(100, fit))
    growth = 50 + (15 if senior_title and level in ("mid", "senior") else 0) + \
        (10 if re.search(r"\b(ai|ml|llm|genai|cloud|architect|platform)\b", t, re.I) else 0)
    budget = salary.parse_budget(jd_text, config.country().get("currency", "INR"),
                                 config.country().get("salary_style", "lpa") if config.country().get("salary_style") == "lpa" else "annual")
    seniority = "Senior" if senior_title else "Junior" if junior_title else "Mid"
    return JobAnalysis(
        title=t[:120] or "unknown", company=company or "unknown", location=location or "not stated",
        seniority=seniority, years_required=(f"{lo:g}-{hi:g} years" if hi else f"{lo:g}+ years") if lo else "not stated",
        must_have=required[:12], nice_to_have=preferred[:10], keywords=(required + preferred)[:25],
        required_skills=required, preferred_skills=preferred, salary_text=budget.text if budget else "not stated",
        responsibilities=[], domain="", fit_score=fit,
        fit_reason="Rule-based read (free mode): " + "; ".join(notes) + ".",
        gaps=miss_r[:10], recommendation="apply" if fit >= 70 else "maybe" if fit >= 55 else "skip",
        role_family=track or "other", realistic=fit >= 55, realism_reason="; ".join(notes), growth_score=min(100, growth),
    )


# --- tailoring ---------------------------------------------------------------------------

def _score(text: str, keywords: list[str]) -> int:
    have, _ = keyword_coverage(keywords, text)
    return len(have)


def tailor(profile, analysis):
    """A TailoredResume from verbatim facts, most relevant first."""
    from jobright.tailor import Line, SkillGroup, TailoredResume, TailoredRole

    kws = list(dict.fromkeys(analysis.required_skills + analysis.preferred_skills + analysis.keywords))
    rank = lambda facts, n: sorted(facts, key=lambda f: -_score(f.text, kws))[:n]
    summary = [Line(text=f.text, source_ids=[f.id]) for f in rank(profile.summary, 3)]
    highlights = [Line(text=f.text, source_ids=[f.id]) for f in rank([*profile.highlights, *profile.practices], 3)
                  if _score(f.text, kws) or len(profile.highlights) <= 3]
    roles = []
    for i, r in enumerate(profile.roles):
        n = 5 if i < 2 else 3 if i < 4 else 2
        keep = rank(r.bullets, n)
        keep.sort(key=lambda f: r.bullets.index(f))           # in the order you wrote them
        projects = sorted(r.projects, key=lambda p: -_score(f"{p.name} {p.summary}", kws))
        roles.append(TailoredRole(role_id=r.id, bullets=[Line(text=f.text, source_ids=[f.id]) for f in keep],
                                  project_ids=[p.id for p in projects[: (2 if i < 2 else 1)]]))
    groups = []
    for name, items in profile.skills.items():
        hits = [s for s in items if _score(s, kws)]
        groups.append((len(hits), name, hits + [s for s in items if s not in hits]))
    groups.sort(key=lambda g: -g[0])
    skills = [SkillGroup(name=n, items=items[:12]) for _, n, items in groups[:5]]
    matched = [k for k in kws if keyword_coverage([k], " ".join(s for g in skills for s in g.items))[0]][:3]
    headline = profile.titles[0] + (" | " + " · ".join(matched) if matched else "") if profile.titles else " · ".join(matched)
    covered, missing = keyword_coverage(kws, " ".join(
        [l.text for l in summary + highlights] + [l.text for r in roles for l in r.bullets] + [s for g in skills for s in g.items]))
    others = sorted(profile.other_projects, key=lambda p: -_score(f"{p.name} {p.summary}", kws))
    return TailoredResume(headline=headline, summary=summary, skills=skills, roles=roles, highlights=highlights,
                          other_project_ids=[p.id for p in others[:2] if _score(f"{p.name} {p.summary}", kws)],
                          keywords_used=covered, gaps=missing)


# --- resume parsing --------------------------------------------------------------------------

_HEAD = {
    "summary": r"(professional )?(summary|profile|about( me)?|objective|personal statement)",
    "experience": r"(work |professional |employment )?(experience|history)|employment|work",
    "education": r"education|academic|qualifications",
    "skills": r"(technical |key |core )?skills|technologies|tech stack|tools|competencies",
    "projects": r"projects|personal projects|portfolio",
    "certifications": r"certifications?|licen[cs]es|courses|awards|achievements",
}
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH}\s*\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_RANGE = re.compile(rf"({_DATE})\s*(?:-|–|—|to)\s*({_DATE}|present|current|now|today)", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _ym(s: str) -> str | None:
    s = s.lower().strip()
    if s in ("present", "current", "now", "today"):
        return None
    m = re.match(rf"(?P<mon>{_MONTH})\s*(?P<year>\d{{4}})", s)
    if m:
        return f"{m.group('year')}-{_MONTHS[m.group('mon')[:3]]:02d}"
    m = re.match(r"(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})", s)
    return f"{m.group(1)}-01" if m else None


def parse_resume(text: str):
    """An XProfile from resume text, by rules. Review it in the wizard."""
    from jobright.onboard import XEducation, XProfile, XProject, XRole, XSkillGroup
    from jobright.profile import Link

    lines = [l.strip() for l in text.splitlines()]
    email = (re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text) or [""])[0]
    phone = (re.search(r"\+?\d[\d ()-]{8,16}\d", text) or [""])[0].strip()
    links = []
    for label, rx in (("LinkedIn", r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+/?"), ("GitHub", r"(?:https?://)?github\.com/[\w-]+/?"),
                      ("Portfolio", r"https?://(?!(?:www\.)?(?:linkedin|github)\.com)[\w.-]+\.[a-z]{2,}[/\w.-]*")):
        m = re.search(rx, text, re.I)
        if m:
            links.append(Link(label=label, url=m.group(0) if m.group(0).startswith("http") else "https://" + m.group(0)))
    name = next((l for l in lines if l and "@" not in l and not re.search(r"\d", l) and len(l.split()) <= 5), "")
    name = name.title() if name.isupper() else name
    loc = ""
    for l in lines[:6]:
        for part in re.split(r"\s*[|•·]\s*", l):
            if "," in part and "@" not in part and not re.search(r"\d", part) and len(part) < 50:
                loc = part
                break
        if loc:
            break

    sections: dict[str, list[str]] = {"header": [], **{k: [] for k in _HEAD}}
    current = "header"           # contact lines until the first heading
    for l in lines[1:]:
        key = next((k for k, rx in _HEAD.items() if re.fullmatch(rf"\s*{rx}\s*:?\s*", l, re.I)), None)
        if key:
            current = key
            continue
        if l:
            sections[current].append(l)

    bullet = re.compile(r"^[•\-–*▪●◦]\s*")
    roles: list[XRole] = []
    for l in sections["experience"]:
        m = _RANGE.search(l)
        if m:
            head = _RANGE.sub("", l).strip(" ()|,-–")
            title, company = head, ""
            for sep in (" at ", " @ ", ", ", " - ", " – ", " | "):
                if sep in head:
                    title, company = [x.strip() for x in head.split(sep, 1)]
                    break
            company = re.sub(r"\s*\(.*$", "", company)
            roles.append(XRole(title=title or "Role", company=company or "", start=_ym(m.group(1)) or "2000-01",
                               end=_ym(m.group(2)), bullets=[],
                               engagement="Internship" if re.search(r"intern", head, re.I) else None))
        elif roles:
            if bullet.match(l) or not roles[-1].company:
                if not roles[-1].company and not bullet.match(l) and not roles[-1].bullets:
                    roles[-1].company = l
                else:
                    roles[-1].bullets.append(bullet.sub("", l))
            else:
                roles[-1].bullets.append(l)
    roles.sort(key=lambda r: r.start, reverse=True)

    education = []
    for l in sections["education"]:
        if re.search(r"\b(bsc|ba|beng|meng|msc|ma|mba|phd|b\.?tech|m\.?tech|bachelor|master|degree|diploma|a.levels|gcse|high school)\b", l, re.I):
            degree, inst = l, ""
            for sep in (", ", " - ", " – ", " | ", " at "):
                if sep in l:
                    degree, inst = [x.strip() for x in l.split(sep, 1)]
                    break
            grade = re.search(r"first class( honours)?|2:1|2:2|third|distinction|merit|cgpa\s*[\d.]+(/\d+)?|gpa\s*[\d.]+", l, re.I)
            years = re.findall(r"\b(19|20)\d{2}\b", l)
            education.append(XEducation(degree=_RANGE.sub("", degree).strip(" ,.-"), institution=re.sub(r"\b(19|20)\d{2}\b.*$", "", inst).strip(" ,.-"),
                                        grade=grade.group(0) if grade else None,
                                        end=re.findall(r"\b(?:19|20)\d{2}\b", l)[-1] if years else None))
        elif education:
            grade = re.search(r"first class( honours)?|2:1|2:2|distinction|merit|cgpa\s*[\d.]+", l, re.I)
            if grade and not education[-1].grade:
                education[-1].grade = grade.group(0)
            education[-1].details.append(bullet.sub("", l))

    skills: list[XSkillGroup] = []
    for l in sections["skills"]:
        if ":" in l:
            group, items = l.split(":", 1)
            skills.append(XSkillGroup(name=bullet.sub("", group).strip(), items=[s.strip() for s in re.split(r",|\||;|•", items) if s.strip()]))
        else:
            items = [s.strip() for s in re.split(r",|\||;|•", bullet.sub("", l)) if s.strip()]
            if items:
                if skills and skills[-1].name == "Skills":
                    skills[-1].items += items
                else:
                    skills.append(XSkillGroup(name="Skills", items=items))
    if not skills:
        found = find_skills(text)
        if found:
            skills = [XSkillGroup(name="Skills", items=found)]

    projects = []
    for l in sections["projects"]:
        l = bullet.sub("", l)
        m = re.match(r"(.{2,60}?)\s*(?:[-–—:|])\s+(.+)", l)
        if m:
            projects.append(XProject(name=m.group(1).strip(), summary=m.group(2).strip()))
        elif projects:
            projects[-1].details.append(l)
    intro = sections["summary"] or [l for l in sections["header"]
                                    if not re.search(r"@|\+?\d[\d ()-]{8,}|linkedin|github|https?://", l, re.I)]
    summary = [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(intro)) if len(s.strip()) > 20][:6]
    professional = [r for r in roles if not r.engagement and not re.search(r"\b(intern|assistant|placement|volunteer|tutor)\b", r.title, re.I)]
    years = 0.0
    if professional:             # earliest professional start to the latest end (or today)
        from datetime import date
        start = min(r.start for r in professional)
        end = max((r.end or date.today().strftime("%Y-%m")) for r in professional)
        years = round((int(end[:4]) - int(start[:4])) + (int(end[5:7]) - int(start[5:7])) / 12, 1)
    titles = [professional[0].title] if professional else []
    if not titles and education:
        titles = [f"{education[0].degree} Graduate"]
    return XProfile(name=name, location=loc, email=email, phone=phone, links=links, titles=titles or ["Candidate"],
                    years_experience=f"{years:g}", summary=summary, skills=skills, roles=roles,
                    other_projects=projects, education=education,
                    certifications=[bullet.sub("", l) for l in sections["certifications"]][:8])


# --- career plan -------------------------------------------------------------------------

_FAMILIES = {
    "mobile": (r"flutter|android|ios|swift|kotlin|react native|mobile|dart", ["{top} Developer", "Mobile Developer", "{level} Mobile Engineer"]),
    "frontend": (r"react|vue|angular|typescript|javascript|css|next\.?js|svelte", ["Frontend Developer", "{level} React Developer", "{level} Frontend Engineer"]),
    "backend": (r"node|express|django|flask|fastapi|spring|java|go|golang|\.net|ruby|php|postgres|mysql|api", ["Backend Developer", "{level} {top} Developer", "{level} Backend Engineer"]),
    "data_ai": (r"python|pandas|sql|machine learning|scikit|tensorflow|pytorch|llm|data", ["Data Analyst", "{level} Data Engineer", "{level} AI Engineer"]),
    "cloud": (r"aws|azure|gcp|docker|kubernetes|terraform|devops|ci/cd", ["Cloud Engineer", "{level} DevOps Engineer"]),
}


def plan_career(profile, country_name: str):
    from jobright.onboard import CareerPlan, Strength, TrackPlan

    years = role_years(profile)
    level = _level(years, profile)
    skills = [s for items in profile.skills.values() for s in items]
    text = " ".join([*skills, *profile.facts().values(), *profile.titles]).lower()
    counts = Counter()
    for s in skills:
        counts[s] = len(re.findall(rf"(?<![a-z]){re.escape(s.lower())}(?![a-z])", text))
    top_skills = [s for s, _ in counts.most_common(10)]
    strengths = [Strength(skill=s, depth=("expert" if c >= 6 and years >= 5 else "solid" if c >= 3 and years >= 2 else
                                          "working" if c >= 2 else "basic"), evidence=f"mentioned {c}x in your profile")
                 for s, c in counts.most_common(8)]
    early = level in ("graduate", "junior")
    lvl_word = {"graduate": "Graduate", "junior": "Junior", "mid": "", "senior": "Senior", "lead": "Lead", "principal": "Principal"}[level]
    word = lambda rx: rf"(?<![a-z])(?:{rx})(?![a-z])"        # "java" is not JavaScript, "go" is not "good"
    scores = {fam: len(re.findall(word(rx), text)) for fam, (rx, _) in _FAMILIES.items()}
    families = [f for f, n in sorted(scores.items(), key=lambda x: -x[1]) if n >= 2][:3] or ["backend"]
    tracks = []
    for i, fam in enumerate(families):
        rx, patterns = _FAMILIES[fam]
        top = next((s for s in top_skills if re.search(word(rx), s.lower())), top_skills[0] if top_skills else "Software")
        kws = [re.sub(r"\s+", " ", p.format(top=top, level=lvl_word)).strip() for p in patterns]
        title_words = sorted({w for w in [top, *re.findall(r"[A-Z][\w.+#]+", " ".join(patterns))] if w not in ("Developer", "Engineer", "{top}", "{level}")} - {"Senior", "Junior"})
        tracks.append(TrackPlan(name=fam, label=f"{lvl_word} {fam.replace('_', ' / ')} roles".strip().capitalize(),
                                titles=list(dict.fromkeys([top, *[t for t in title_words if len(t) > 2]]))[:6],
                                search_keywords=kws, weight=1.0 if i == 0 else 0.9, min_fit=50 if i == 0 else 58,
                                stretch=i > 0, why=f"{scores[fam]} mentions of {fam.replace('_', ' ')} skills in your profile"))
    if early:
        tracks.insert(0, TrackPlan(name="graduate", label="Graduate and junior software roles",
                                   titles=["Graduate", "Junior", "Associate", "Entry Level", "Software Engineer", "Developer"],
                                   search_keywords=["Graduate Software Engineer", "Junior Developer", "Graduate Scheme Technology"],
                                   weight=1.0, min_fit=50, stretch=False, why="Your experience level"))
    if re.search(r"\b(led|lead|mentor|managed|manager)\b", text) and level in ("senior", "lead", "principal"):
        tracks.append(TrackPlan(name="leadership", label="Engineering lead / manager",
                                titles=["Engineering Manager", "Tech Lead", "Technical Lead", "Lead Engineer"],
                                search_keywords=["Engineering Manager", "Tech Lead"], weight=1.05, min_fit=58, stretch=True,
                                why="Your profile shows leading or mentoring"))
    exclude = ["Senior", "Lead", "Principal", "Staff", "Manager", "Head", "Director"] if early else \
              ["Intern", "Internship", "Graduate", "Junior", "Trainee", "Fresher"]
    return CareerPlan(level=level, years=years, primary=", ".join(top_skills[:3]) or "Software",
                      summary=f"{lvl_word or 'Mid-level'} profile with about {years:g} years of dated experience in {country_name}; "
                              f"strongest in {', '.join(top_skills[:4]) or 'your listed skills'}. (Rule-based plan: add a Claude key "
                              f"or Ollama for a deeper read.)",
                      strengths=strengths, transferable=[], growth_areas=[], tracks=tracks[:6], title_exclude=exclude)
