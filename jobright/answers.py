"""Answers for application-form fields.

For each field: a saved answer you gave for the same question wins; else
the field is interpreted (rules, then Claude for wording the rules do not
know, cached) and the fact is resolved from data/answers.yaml or the master
profile, rendered in the field's type and unit, and validated.

Claude only says what a question is asking (intent, skill, unit). It never
supplies the answer, so nothing personal can be invented: a question the
answer bank and profile cannot answer goes to your Questions page and the
job to your review queue. Answer it once and every later form reuses it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml
from pydantic import BaseModel

from jobright import config
from jobright.apply.fields import PERSONAL, FieldSpec, Intent, Interpretation, classify, norm
from jobright.apply.values import Fact, JobContext, pick, render, resolve, unset, validate

SAVED_PATH = config.DATA / "saved_answers.yaml"
PENDING_PATH = config.DATA / "pending_questions.yaml"
FIELD_CACHE_PATH = config.DATA / "field_cache.yaml"

__all__ = ["JobContext", "Resolution", "answer_field", "answer", "bank", "saved", "save_answer",
           "pending_questions", "add_pending", "norm_q", "base_question"]


def norm_q(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", q.lower().replace("*", "")).strip()


def _load_yaml(path) -> Any:
    if not path.exists():
        return None
    with path.open() as f:
        return yaml.safe_load(f)


def _write_yaml(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def bank() -> dict:
    return _load_yaml(config.ANSWERS_PATH) or {}


def saved() -> dict[str, str]:
    return _load_yaml(SAVED_PATH) or {}


def save_answer(question: str, answer: str) -> None:
    data = saved()
    data[norm_q(question)] = answer
    _write_yaml(SAVED_PATH, dict(sorted(data.items())))
    pending = [p for p in pending_questions() if norm_q(p["question"]) != norm_q(question)]
    _write_yaml(PENDING_PATH, pending)


_NOTE = re.compile(r"\s*\((site says|site pre-filled|could not fill|could not type|Naukri did not).*$")


def base_question(missing: str) -> str:
    """A blocking question without the bot's note on why it stopped there."""
    return _NOTE.sub("", missing)


def pending_questions() -> list[dict]:
    return _load_yaml(PENDING_PATH) or []


def add_pending(question: str, kind: str, options: list[str], job_id: str | None,
                intent: str | None = None, reason: str | None = None) -> None:
    items = pending_questions()
    key = norm_q(question)
    for item in items:
        if norm_q(item["question"]) == key:
            item["count"] = item.get("count", 1) + 1
            jobs = set(item.get("jobs") or []) | ({job_id} if job_id else set())
            item["jobs"] = sorted(jobs)
            break
    else:
        items.append({"question": question.strip(), "kind": kind, "options": options,
                      "intent": intent, "reason": reason, "jobs": [job_id] if job_id else [],
                      "count": 1, "first_seen": datetime.now().isoformat(timespec="minutes")})
    _write_yaml(PENDING_PATH, items)


# --- interpretation ----------------------------------------------------------

class _FieldReading(BaseModel):
    intent: str
    subject: str | None
    unit: str | None


def _cache_key(spec: FieldSpec) -> str:
    return norm_q(spec.label) + ("|" + "/".join(norm_q(o) for o in spec.options[:8]) if spec.options else "")


def _model_interpret(spec: FieldSpec) -> Interpretation:
    """Ask Claude what an unfamiliar question is asking. Cached per wording."""
    cache = _load_yaml(FIELD_CACHE_PATH) or {}
    key = _cache_key(spec)
    if key in cache:
        c = cache[key]
        return Interpretation(Intent(c["intent"]), c.get("subject"), c.get("unit"), 0.8, "cache")
    if os.environ.get("JOBRIGHT_OFFLINE"):
        return Interpretation(Intent.UNKNOWN, confidence=0.0)
    from jobright.llm import LLM, LLMError

    prompt = (
        f"<field kind=\"{spec.kind}\" required=\"{spec.required}\">\n"
        f"<label>{spec.label}</label>\n<hint>{spec.hint}</hint>\n"
        + (f"<options>{spec.options}</options>\n" if spec.options else "")
        + (f"<error>{spec.error}</error>\n" if spec.error else "")
        + "</field>\n<intents>" + ", ".join(x.value for x in Intent) + "</intents>"
    )
    try:
        r = LLM().structured(purpose="field", model=config.settings()["models"]["answer"],
                             instructions=config.prompt("field"), context="(no profile needed)",
                             prompt=prompt, schema=_FieldReading, effort="low", max_tokens=2000)
        intent = Intent(r.intent) if r.intent in {x.value for x in Intent} else Intent.UNKNOWN
    except (Exception,):  # a failed lookup only means "ask the user"
        return Interpretation(Intent.UNKNOWN, confidence=0.0)
    cache[key] = {"intent": intent.value, "subject": r.subject, "unit": r.unit}
    _write_yaml(FIELD_CACHE_PATH, cache)
    return Interpretation(intent, r.subject, r.unit, 0.8, "model")


def interpret(spec: FieldSpec, ctx: JobContext) -> Interpretation:
    interp = classify(spec, ctx.siblings)
    # confidence 1 on unknown: the rules know this one is yours to answer, so no model reading
    if interp.intent == Intent.UNKNOWN and interp.confidence < 1 and spec.label.strip():
        interp = _model_interpret(spec)
    return interp


# --- answering ---------------------------------------------------------------

@dataclass
class Resolution:
    value: str                     # what to type, or the option to pick
    candidates: list[str]          # alternatives for error recovery
    interp: Interpretation
    source: str                    # audit: where the value came from


def answer_field(spec: FieldSpec, ctx: JobContext, record_missing: bool = True) -> Resolution | None:
    label = spec.label or spec.name
    b, s = bank(), saved()
    interp = interpret(spec, ctx)

    fact: Fact | None = None
    if norm_q(label) in s and not unset(s[norm_q(label)]):
        fact = Fact(s[norm_q(label)], "saved answer")
    else:
        fact = resolve(interp, spec, ctx, b, s)

    choice_kind = spec.kind in ("select", "radio", "checkbox_group", "combobox")
    if fact is None and spec.options and interp.intent in (Intent.EEO, Intent.GENDER):
        decline = next((o for o in spec.options if re.search(r"decline|prefer not|don.?t wish|not to say", o, re.I)), None)
        if decline:
            return Resolution(decline, [decline], interp, "voluntary question: declined")

    if fact is None:
        if record_missing:
            reason = next((f for f in ctx.flags if f.startswith("salary review")), None)
            if not reason:
                reason = "personal detail: only you can answer" if interp.intent in PERSONAL else \
                         "not in your answer bank or profile"
            add_pending(label, spec.kind, spec.options, ctx.job_id, interp.intent.value, reason)
        return None

    candidates = render(fact, spec, interp, ctx)
    if choice_kind and spec.options:
        picked = [o for o in (pick(spec.options, c) for c in candidates) if o]
        if not picked:
            if record_missing:
                add_pending(label, spec.kind, spec.options, ctx.job_id, interp.intent.value,
                            f"your answer '{candidates[0]}' is not one of the options")
            return None
        candidates = list(dict.fromkeys(picked))
    if spec.kind == "combobox" and not spec.options:
        return Resolution(candidates[0], candidates, interp, fact.source)   # options appear once opened

    valid = [c for c in candidates if not validate(c, spec, interp)]
    if not valid:
        if record_missing:
            add_pending(label, spec.kind, spec.options, ctx.job_id, interp.intent.value,
                        f"'{candidates[0]}' does not fit this field ({validate(candidates[0], spec, interp)})")
        return None
    return Resolution(valid[0], valid, interp, fact.source)


def answer(label: str, kind: str = "text", options: list[str] | None = None,
           job_id: str | None = None, ctx: JobContext | None = None) -> str | None:
    """One-shot answer for a question outside a scanned form (e.g. Naukri's chat)."""
    ctx = ctx or JobContext(job_id=job_id)
    r = answer_field(FieldSpec(n="-", kind=kind, label=label, options=options or []), ctx)
    return r.value if r else None
