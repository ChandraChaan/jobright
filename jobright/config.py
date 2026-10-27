"""Where a user's files live, and their settings.

Everything personal lives in one folder, the jobright home:
  $JOBRIGHT_HOME, if set; else ./data in a development checkout that already
  has one; else ~/.jobright.
It holds settings.yaml (your overrides of jobright/defaults/settings.yaml),
master_profile.yaml, answers.yaml, the job database, generated resumes,
screenshots, logs, the bot's browser profile and .env (your API keys). The
code itself carries no personal data.
"""

from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
DEFAULTS_DIR = PACKAGE / "defaults"
PROMPTS_DIR = PACKAGE / "prompts"
TEMPLATES_DIR = PACKAGE / "templates"


def _home() -> Path:
    env = os.environ.get("JOBRIGHT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    dev = ROOT / "data"
    if (dev / "master_profile.yaml").exists() or (dev / "settings.yaml").exists():
        return dev
    return Path.home() / ".jobright"


DATA = _home()
PROFILE_PATH = DATA / "master_profile.yaml"
ANSWERS_PATH = DATA / "answers.yaml"
CAREER_PATH = DATA / "career.yaml"
SETTINGS_PATH = DATA / "settings.yaml"
ENV_PATH = DATA / ".env"
DB_PATH = Path(os.environ.get("JOBRIGHT_DB", DATA / "jobright.db"))
APPLICATIONS_DIR = DATA / "applications"
BROWSER_PROFILE = DATA / "browser-profile"
LOGS_DIR = DATA / "logs"

load_dotenv(ENV_PATH)
load_dotenv(ROOT / ".env")   # older checkouts kept it at the repo root


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in _REPLACE:
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# Keys whose user value replaces the default wholesale instead of merging into it.
_REPLACE = {"tracks", "searches", "ats_boards"}


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    user = _load(SETTINGS_PATH) or _load(ROOT / "config" / "settings.yaml")   # older layout
    return _merge(_load(DEFAULTS_DIR / "settings.yaml"), user)


def user_settings() -> dict[str, Any]:
    return _load(SETTINGS_PATH)


def save_settings(changes: dict[str, Any]) -> None:
    """Merge `changes` into the user's settings.yaml and reload."""
    current = _merge(user_settings(), changes)
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(yaml.safe_dump(current, sort_keys=False, allow_unicode=True))
    settings.cache_clear()


@lru_cache(maxsize=1)
def countries() -> dict[str, Any]:
    return _load(DEFAULTS_DIR / "countries.yaml")


def country() -> dict[str, Any]:
    """Settings for the user's country (currency, job sites, LinkedIn geo ids…)."""
    code = (settings().get("profile") or {}).get("country") or "IN"
    return {"code": code, **countries().get(code, countries().get("OTHER", {}))}


def set_env(key: str, value: str) -> None:
    """Store a secret in the user's .env (readable only by the user)."""
    DATA.mkdir(parents=True, exist_ok=True)
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")] + [f"{key}={value}"]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass
    os.environ[key] = value


def prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()
