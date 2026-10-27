"""Submitting applications. Every engine returns an Outcome; none raises for
ordinary failures, so one bad form never stops a run.

Outcome statuses:
  applied      submitted, and the site confirmed it (evidence says how)
  unverified   Submit was pressed but no confirmation was seen; re-checked
               on the next run before anything is retried, so it is never
               submitted twice
  unavailable  the posting is removed, closed or expired
  needs_you    a question only you can answer, an unfamiliar site, or a step
               the bot could not get past (details in `missing` / `detail`)
  blocked      security check, CAPTCHA or signed out: the source is paused
               for the rest of the run
  failed       an unexpected error (retried later, up to the attempt limit)
  dry_run      a rehearsal that stopped before Submit
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Outcome:
    status: str
    detail: str = ""
    missing: list[str] = field(default_factory=list)
    screenshot: str | None = None
    evidence: str = ""                                   # what proved the submission
    audit: list[dict] = field(default_factory=list)      # answers given, with their sources
    notes: list[str] = field(default_factory=list)       # popups closed, fields corrected, recoveries
