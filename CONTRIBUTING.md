# Contributing to jobright

Thanks for helping. The most useful contributions:

- **A new job site or country.** See [docs/adding-a-site.md](docs/adding-a-site.md). Most sites need a source in `jobright/sources/` and either reuse `apply/stepper.py` or get a small engine in `jobright/apply/`.
- **Form patterns.** If a real form question was misunderstood, add the label to `tests/test_fields.py` with the intent it should map to, then fix `jobright/apply/fields.py`.
- **Bug reports with a screenshot.** Screenshots are saved in `~/.jobright/screenshots`. **Blur personal details before posting.**

## Ground rules

1. **Never invent facts about the user.** Resume text comes only from the master profile, and personal answers only from the answer bank. If a change makes the agent guess, it won't be merged.
2. **No CAPTCHA solving or bot-detection evasion.** The agent stops at security checks. Keep the per-site daily limits and pauses.
3. **No personal data in the repo.** Tests use the fictional person in `tests/fixtures/home`. `scripts/check_privacy.py` runs in CI.

## Development

```bash
git clone https://github.com/ChandraChaan/jobright && cd jobright
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
pytest
JOBRIGHT_HOME=/tmp/jobright-dev jobright start   # try the setup wizard with a throwaway folder
```
