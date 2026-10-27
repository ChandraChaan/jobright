"""Which brain jobright uses: free rules, a free local model, or Claude.

  free   No AI and no key. Screening, tailoring, resume import and the career
         plan run on rules and your own facts (see heuristics.py). $0.
  local  A model running on your own computer through Ollama (ollama.com):
         reads job descriptions and unfamiliar form questions. Resumes stay
         word-for-word from your facts. $0, private.
  claude Your Claude API key: rewritten, fact-checked resumes and deep fit
         checks. Best quality; you pay Anthropic for usage.

settings.ai.engine: auto (default) picks claude when a key is set, else local
when Ollama is running, else free.
"""

from __future__ import annotations

import json
import os
import urllib.request
from functools import lru_cache

from jobright import config

ENGINES = ("free", "local", "claude")


def ollama_url() -> str:
    return (config.settings().get("ai") or {}).get("ollama_url") or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"


def local_model() -> str:
    return (config.settings().get("ai") or {}).get("local_model") or "qwen2.5:7b"


@lru_cache(maxsize=1)
def ollama_models() -> tuple[str, ...]:
    """Models installed in Ollama, or () when Ollama is not running."""
    try:
        with urllib.request.urlopen(ollama_url().rstrip("/") + "/api/tags", timeout=2) as r:
            return tuple(m["name"] for m in json.load(r).get("models", []))
    except Exception:
        return ()


def current() -> str:
    if os.environ.get("JOBRIGHT_OFFLINE"):
        return "free"
    choice = (config.settings().get("ai") or {}).get("engine") or "auto"
    if choice in ENGINES:
        if choice == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            return "local" if ollama_models() else "free"
        if choice == "local" and not ollama_models():
            return "free"
        return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "local" if ollama_models() else "free"


def uses_ai() -> bool:
    return current() != "free"


def rewrites_resumes() -> bool:
    """Only Claude rewrites resume lines; free and local keep your exact wording."""
    if current() == "claude":
        return True
    return current() == "local" and bool((config.settings().get("ai") or {}).get("local_tailoring"))


def describe() -> str:
    return {"free": "Free mode (rules, no AI, $0)",
            "local": f"Local AI ({local_model()} via Ollama, $0)",
            "claude": "Claude (your API key)"}[current()]
