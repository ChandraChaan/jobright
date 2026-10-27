"""The bot's own browser profile for the job sites that need a login.

It lives in browser-profile/ inside your jobright folder and keeps the
sessions you create with `jobright login <site>`. The bot never sees or
stores your passwords: you sign in yourself in that window, once.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from jobright import config

# Older checkouts kept the profile at the repo root; use it until it is moved.
PROFILE_DIR = config.BROWSER_PROFILE if config.BROWSER_PROFILE.exists() or not (config.ROOT / "browser-profile").exists() \
    else config.ROOT / "browser-profile"

SITES = ("linkedin", "naukri", "reed", "indeed")


def _indeed() -> str:
    return config.country().get("indeed_domain") or "www.indeed.com"


LOGIN_URLS = {
    "linkedin": lambda: "https://www.linkedin.com/login",
    "naukri": lambda: "https://www.naukri.com/nlogin/login",
    "reed": lambda: "https://www.reed.co.uk/account/signin",
    "indeed": lambda: "https://secure.indeed.com/auth",
}
# A page only a signed-in user reaches, and the URL fragment that proves it.
LOGGED_IN_CHECK = {
    "linkedin": (lambda: "https://www.linkedin.com/feed/", "/feed"),
    "naukri": (lambda: "https://www.naukri.com/mnjuser/homepage", "/mnjuser"),
    "reed": (lambda: "https://www.reed.co.uk/account", "/account"),
    "indeed": (lambda: f"https://{_indeed()}/myjobs", "myjobs"),
}
# Session cookies a site sets only after a successful sign-in, where known.
SESSION_COOKIES = {
    "linkedin": ("linkedin.com", {"li_at"}),
    "naukri": ("naukri.com", {"nauk_at", "nauk_rt"}),
}
# For sites without a known cookie: the login page's URL fragment, which the
# browser leaves once sign-in succeeds.
LOGIN_FRAGMENT = {"reed": "/account/signin", "indeed": "secure.indeed.com/auth"}


def has_session(ctx: BrowserContext, site: str) -> bool:
    if site not in SESSION_COOKIES:
        return True   # checked by navigation in is_logged_in
    domain, names = SESSION_COOKIES[site]
    try:
        cookies = ctx.cookies()
    except Exception:
        return False
    return any(c["name"] in names and domain in c["domain"] for c in cookies)


def _chrome_installed() -> bool:
    return Path("/Applications/Google Chrome.app").exists() or bool(shutil.which("google-chrome"))


@contextlib.contextmanager
def open_browser(headless: bool | None = None):
    """Yield a persistent context on the bot's profile."""
    if headless is None:
        headless = config.settings()["apply"].get("browser_headless", False)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1366, "height": 900},
            accept_downloads=False,
            # No "allow notifications / location?" prompts on top of forms.
            args=["--disable-notifications", "--deny-permission-prompts"],
            permissions=[],
        )
        if _chrome_installed():
            kwargs["channel"] = "chrome"
        ctx: BrowserContext = p.chromium.launch_persistent_context(**kwargs)
        from jobright.apply.guard import install
        for pg in ctx.pages:
            install(pg)
        ctx.on("page", install)
        try:
            yield ctx
        finally:
            ctx.close()


def page(ctx: BrowserContext):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def is_logged_in(ctx: BrowserContext, site: str) -> bool:
    if not has_session(ctx, site):
        return False
    url, marker = LOGGED_IN_CHECK[site]
    pg = page(ctx)
    pg.goto(url(), wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    # A signed-out visit lands on the login page, whose URL may contain the marker too.
    return marker in pg.url and not (site in LOGIN_FRAGMENT and LOGIN_FRAGMENT[site] in pg.url)


def login(site: str) -> None:
    """Open the site's login page in the bot's browser and wait for you to sign in.

    Done when the site's session cookie appears. Closing the window early is
    fine: the profile keeps whatever session exists, and `jobright session`
    tells you whether it is there.
    """
    from playwright.sync_api import Error as PlaywrightError

    with open_browser(headless=False) as ctx:
        pg = page(ctx)
        if site in SESSION_COOKIES and has_session(ctx, site):
            print(f"Already signed in to {site}.")
            return
        pg.goto(LOGIN_URLS[site]())
        print(f"Sign in to {site} in the window that opened (OTP / 2-step is fine). Waiting up to 10 minutes…")
        signed_in = (lambda: has_session(ctx, site)) if site in SESSION_COOKIES else \
            (lambda: LOGIN_FRAGMENT[site] not in pg.url and pg.url.startswith("http"))
        try:
            pg.wait_for_timeout(3000)
            for _ in range(600):
                if signed_in():
                    print(f"Signed in to {site}. The session is saved in your jobright folder; you can close the window.")
                    pg.wait_for_timeout(4000)  # let the site finish writing its cookies
                    return
                pg.wait_for_timeout(1000)
            print("Timed out; run the command again when ready.")
        except PlaywrightError:
            print("The window was closed before sign-in finished. Run the command again to sign in.")


def session_status(sites: list[str] | None = None) -> dict[str, bool]:
    sites = sites or [x for x in ((config.settings().get("profile") or {}).get("sites") or list(SESSION_COOKIES)) if x in SITES]
    with open_browser(headless=True) as ctx:
        return {site: (has_session(ctx, site) if site in SESSION_COOKIES else is_logged_in(ctx, site)) for site in sites}
