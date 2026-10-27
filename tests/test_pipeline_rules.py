"""Free checks that decide what gets looked at, and duplicate detection."""

from jobright.apply.values import pick
from jobright.coverage import keyword_coverage
from jobright.jd import years_band
from jobright.jd import JobAnalysis
from jobright.runner import filter_reason, judge, priority_score, track_of
from jobright.store import dedupe_key, norm_title


def test_title_and_location_filters():
    assert filter_reason("Senior Flutter Developer", "SmartQ", "Bengaluru") is None
    assert filter_reason("Staff Software Engineer", "Stripe", "Bengaluru, India") is None
    assert filter_reason("Staff Software Engineer - Backend", "Databricks", "Bengaluru") is None   # judged on fit now
    assert filter_reason("Legal Operations Team Lead", "Stripe", "Bengaluru") == "title excluded"
    assert filter_reason("Data Scientist", "Acme", "Bengaluru") == "title excluded"
    assert filter_reason("Python Automation Engineer", "Acme", "Bengaluru").startswith("title does not match")
    assert filter_reason("Lead Mobile Engineer", "Coinbase", "Remote - California").startswith("location")
    assert filter_reason("Lead Mobile Engineer", "Acme Learning", "Bangalore") == "company excluded"
    assert filter_reason("Technical Architect", "Acme", "Remote", source="naukri") is None
    assert filter_reason("Technical Architect", "Acme", "Remote", source="linkedin") is None      # search was country-limited
    assert filter_reason("Technical Architect", "Acme", "Remote", source="greenhouse").startswith("location")  # boards are global


def test_same_job_on_two_sites_is_one_job():
    assert dedupe_key("Razorpay Software Private Limited", "Lead Mobile Engineer") == \
        dedupe_key("Razorpay", "Lead Mobile Engineer")
    assert norm_title("Sr. Flutter Developer - Bangalore") == norm_title("Senior Flutter Developer (Hybrid)")
    # different roles at one company stay different
    assert norm_title("Staff Software Engineer - Backend") != norm_title("Staff Software Engineer - Frontend")
    assert norm_title("Lead Engineer - Full Stack (Mobile)") != norm_title("Lead Engineer")


def test_option_matching_prefers_strong_matches():
    countries = ["British Indian Ocean Territory +246", "India +91", "Indonesia +62"]
    assert pick(countries, "India") == "India +91"
    assert pick(["0-15 days", "15-30 days", "30-60 days", "60+ days"], "45") == "30-60 days"
    assert pick(["Yes", "No"], "yes") == "Yes"
    assert pick(["15 days or less", "1 month", "2 months", "3 months"], "14 days") == "15 days or less"
    assert pick(["0-15 days", "15-30 days"], "14 days") == "0-15 days"
    assert pick(["Male", "Female", "Others"], "male") == "Male"


def test_keyword_coverage_folds_plurals_synonyms_and_filler():
    text = "Led code reviews for mobile application development; RESTful APIs; CI CD with GitHub Actions; Android."
    have, missing = keyword_coverage(["Code review", "Mobile app development", "REST APIs", "CI/CD",
                                      "Android Application Development", "Jira"], text)
    assert missing == ["Jira"]
    assert len(have) == 5


def test_years_band():
    assert years_band("3-5 Yrs") == (3, 5)
    assert years_band("not stated", "We need 7+ years of experience") == (7, None)
    assert years_band("Minimum 5 years") == (5, None)
    assert years_band("not stated", "") == (None, None)


def test_tracks():
    assert track_of("Lead Flutter Developer") == "mobile"
    assert track_of("Engineering Manager - Platform") == "leadership"
    assert track_of("Solutions Architect (AWS)") == "architecture"
    assert track_of("GenAI Engineer") == "ai_applications"
    assert track_of("Full Stack Developer (React/Node)") == "fullstack"
    assert track_of("Senior Node.js Developer") == "backend_cloud"
    assert track_of("Accountant") is None


def test_priority_prefers_fit_growth_and_pay():
    t = {"weight": 1.0}
    strong = priority_score(t, fit=85, skill=80, growth=80, budget_max=60, expected=40, current=30)
    middle = priority_score(t, fit=85, skill=80, growth=80, budget_max=35, expected=40, current=30)
    unknown = priority_score(t, fit=85, skill=80, growth=80, budget_max=None, expected=40, current=30)
    weak_pay = priority_score(t, fit=85, skill=80, growth=80, budget_max=25, expected=40, current=30)
    assert strong > middle > unknown > weak_pay
    assert priority_score({"weight": 1.1}, 85, 80, 80, 60, 40, 30) > strong


def _analysis(**kw):
    base = dict(title="GenAI Engineer", company="Acme", location="Bengaluru", seniority="Senior",
                years_required="5+ years", must_have=[], nice_to_have=[], keywords=[],
                required_skills=["Flutter", "Node.js", "AWS", "Python", "LangChain"], responsibilities=[],
                domain="AI", fit_score=72, fit_reason="", gaps=[], recommendation="apply",
                role_family="ai_applications", realistic=True, realism_reason="builds AI features in apps", growth_score=85)
    base.update(kw)
    return JobAnalysis(**base)


def test_stretch_track_needs_higher_fit_and_realism():
    job = {"title": "GenAI Engineer", "company": "Acme", "description": "Build LLM apps. 5+ years.", "track": "ai_applications"}
    ptext = "Flutter Node.js AWS serverless REST APIs Firebase"
    status, _, f = judge(job, _analysis(), ptext)
    assert status == "screened" and f["track"] == "ai_applications"
    assert judge(job, _analysis(fit_score=58), ptext)[0] == "skipped"          # below the AI track's bar of 60
    assert judge(job, _analysis(realistic=False), ptext)[0] == "skipped"
    mobile = {**job, "title": "Flutter Developer", "track": "mobile"}
    assert judge(mobile, _analysis(fit_score=58), ptext)[0] == "screened"      # core track bar is 50
