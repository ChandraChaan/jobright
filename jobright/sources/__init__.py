"""Where jobs come from. Each source yields Posting objects; the runner stores them."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass
class Posting:
    source: str
    external_id: str
    url: str
    title: str
    company: str
    location: str = ""
    description: str = ""
    apply_url: str | None = None
    apply_type: str | None = None
    posted_at: str | None = None


def html_to_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()
