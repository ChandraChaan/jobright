"""How many of a job's skills or ATS keywords appear in a resume's text.

A keyword counts as covered when each of its words appears, in any order,
within a short window of the resume text. Plurals, "app"/"application" and
common equivalent spellings are folded together first, so "Code review"
matches "code reviews", "RESTful APIs" matches "REST APIs" and "Android
Application Development" matches "Android".
"""

from __future__ import annotations

import re

_STOP = {"and", "or", "of", "in", "the", "a", "an", "with", "for", "on", "to", "using", "based"}
_FOLD = {"app": "application", "apps": "application", "dev": "development", "restful": "rest",
         "apis": "api", "js": "javascript", "golang": "go", "k8s": "kubernetes", "ts": "typescript",
         "ios": "ios", "iphone": "ios", "gcp": "google cloud", "cicd": "ci/cd"}
# Phrases rewritten before tokenising (applied to both the keyword and the resume).
_PHRASES = [
    (r"\bci\s*/?\s*cd\b|\bcontinuous integration\b", "ci/cd"),
    (r"\bnode\s*\.?\s*js\b", "nodejs"),
    (r"\bobjective\s*-?\s*c\b", "objectivec"),
    (r"\breact\s*native\b", "reactnative"),
    (r"\bb\.?\s?tech\b\.?", "btech"),
    (r"\bunit\s*tests?\b", "unit testing"),
    (r"\bwidget\s*tests?\b", "widget testing"),
    (r"\bintegration\s*tests?\b", "integration testing"),
    # "Android Application Development" asks for Android: drop the filler words.
    (r"\b(android|ios|flutter|mobile|web|frontend|backend|cross platform)\s+(application\s+|app\s+)?(development|developer|engineering)\b", r"\1"),
    (r"\b(mobile|android|ios)\s+(applications?|apps?)\b", r"\1"),
]
_WINDOW = 8


def _prep(s: str) -> str:
    s = s.lower().replace("-", " ")
    for pattern, repl in _PHRASES:
        s = re.sub(pattern, repl, s)
    return s


def _tokens(s: str) -> list[str]:
    words = re.findall(r"[a-z0-9+#/.]+", _prep(s))
    out = []
    for w in words:
        w = w.strip(".")
        w = _FOLD.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w and w not in _STOP:
            out.extend(w.split())
    return out


def _covered(keyword: list[str], text: list[str]) -> bool:
    if not keyword:
        return True
    need = set(keyword)
    for i, tok in enumerate(text):
        if tok in need and need <= set(text[max(0, i - _WINDOW): i + _WINDOW]):
            return True
    return False


def keyword_coverage(keywords: list[str], text: str) -> tuple[list[str], list[str]]:
    body = _tokens(text)
    covered, missing = [], []
    for kw in keywords:
        (covered if _covered(_tokens(kw), body) else missing).append(kw)
    return covered, missing
