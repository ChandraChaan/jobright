"""Fail if tracked files contain personal data.

Checks every file git tracks for API keys, email addresses outside
example.com, phone numbers that are not obvious dummies, and any term listed
in a private word list kept OUTSIDE the repo (one term per line):

    python scripts/check_privacy.py --terms ~/.jobright/private_terms.txt
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w.])\+?\d[\d \-]{9,14}\d(?![\w.])")
SAFE_EMAIL = re.compile(r"@(example\.(com|org|net)|users\.noreply\.github\.com|anthropic\.com)$|^noreply@", re.I)
SAFE_PHONE = re.compile(r"^\+?(91 ?9000000\d{3}|44 ?7700 ?900\d{3}|0+|1234567890|9876543210)$")
SKIP = re.compile(r"(\.png|\.jpg|\.gif|\.webp|\.pdf|\.lock|package-lock\.json)$|^scripts/check_privacy\.py$")


def tracked() -> list[str]:
    return subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", type=Path, help="private word list (kept outside the repo)")
    args = ap.parse_args()
    terms = []
    if args.terms and args.terms.exists():
        terms = [t.strip() for t in args.terms.read_text().splitlines() if t.strip() and not t.startswith("#")]
    problems = []
    for path in tracked():
        if SKIP.search(path) or not Path(path).is_file():
            continue
        try:
            text = Path(path).read_text(errors="ignore")
        except Exception:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if KEY.search(line):
                problems.append((path, n, "API key"))
            for m in EMAIL.finditer(line):
                if not SAFE_EMAIL.search(m.group(0)):
                    problems.append((path, n, f"email {m.group(0)}"))
            for m in PHONE.finditer(line):
                number = re.sub(r"[ \-]", "", m.group(0))
                # "+<country> …", or a bare 10-digit mobile (Indian mobiles start 6-9). Long
                # numeric ids (job ids) start with other digits and are not flagged.
                phone_like = number.startswith("+") or re.fullmatch(r"[6-9]\d{9}", number)
                if phone_like and not SAFE_PHONE.match(number):
                    problems.append((path, n, f"phone-like {m.group(0)}"))
            low = line.lower()
            for t in terms:
                if t.lower() in low:
                    problems.append((path, n, f"private term {t!r}"))
    for path, n, what in problems:
        print(f"{path}:{n}: {what}")
    print(f"{len(problems)} problem(s) in {len(tracked())} tracked files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
