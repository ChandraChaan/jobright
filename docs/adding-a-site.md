# Adding a job site

A site needs up to three pieces. Look at Reed (API) and Indeed (browser) for examples.

## 1. A source: `jobright/sources/<site>.py`

Yield `Posting` objects for new jobs:

```python
def discover(searches, known, title_ok=lambda t: True, log=print) -> Iterator[Posting]:
    ...
    yield Posting(source="mysite", external_id=job_id, url=url, apply_url=url,
                  apply_type="mysite",            # or "external" when it sends you to the company
                  title=title, company=company, location=location,
                  description=text)               # include a "Salary: ..." line if the site shows pay
```

- `known(external_id)` is true for jobs already stored, so skip those before opening their pages.
- `title_ok(title)` applies the user's track and exclusion rules for free, so check it on the card before opening the job.
- Prefer an official API. With a browser, pause 2–5 s between pages, and stop the site for the run when `guard.page_issue()` reports a security check.

Wire it into `runner.discover()` and add the site to the right countries in `defaults/countries.yaml`.

## 2. Applying

Most sites are a multi-step form. Describe the buttons and the confirmation text, and reuse the stepper:

```python
MYSITE = stepper.Site(name="mysite", start=re.compile(r"^apply now$", re.I),
                      submit=re.compile(r"^submit application$", re.I),
                      advance=re.compile(r"^(continue|next)$", re.I),
                      done=re.compile(r"application (has been )?sent", re.I),
                      applied=re.compile(r"you applied", re.I))
```

Route `apply_type == "mysite"` to `stepper.run(page, job, pdf, MYSITE, ...)` in `apply/dispatch.py`. The stepper already clears popups, fills fields through the answer engine, recovers from validation errors, detects stuck steps, and returns `unverified` (never `failed`) when Submit was pressed without a visible confirmation.

## 3. Sign-in (if the site needs it)

Add the login URL, a signed-in-only page and the login-page URL fragment to `browser.py`, then `jobright login mysite` works.

## Test it

Add a local HTML mock to `tests/fixtures/` (see `easy_apply.html`) that reproduces the site's steps, popups and errors, and a test that runs the real engine against it. No real accounts or personal data in tests.
