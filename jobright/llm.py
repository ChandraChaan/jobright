"""Claude API access: structured output, prompt caching, refusal fallback, cost log."""

from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from jobright import store

T = TypeVar("T", bound=BaseModel)

# USD per million tokens: input, output, cache read, cache write (5-minute TTL).
# Source: Anthropic pricing, Oct 2026. Re-check https://www.anthropic.com/pricing
# if the dashboard's spend drifts from the console's.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}

# Haiku 5.5 has no server-side fallback; the others re-run a declined request
# on Anthropic's recommended fallback model inside the same call.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self) -> None:
        try:
            self.client = anthropic.Anthropic(max_retries=4)
        except anthropic.AnthropicError as e:
            raise LLMError(
                "No Anthropic credentials. Put ANTHROPIC_API_KEY in job-right/.env"
            ) from e

    def structured(self, *, purpose: str, model: str, instructions: str, context: str,
                   prompt: str, schema: type[T], effort: str = "high",
                   max_tokens: int = 16000) -> T:
        """One request whose reply is validated against `schema`.

        `instructions` and `context` (the master profile) are the stable prefix
        and are cached, so repeated calls for different jobs read them at the
        cache price. `prompt` carries the per-job content.
        """
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": instructions},
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": effort},
        )
        if model in _FALLBACK_MODELS:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            response = self.client.beta.messages.parse(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("ANTHROPIC_API_KEY was rejected; check job-right/.env") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Rate limited on {model} after retries: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code} on {purpose}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Claude API: {e}") from e

        self._record(purpose, model, response.usage)

        if response.stop_reason == "refusal":
            raise LLMError(f"{purpose}: request declined by {model} and its fallback")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: reply hit max_tokens={max_tokens}")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"{purpose}: reply did not match {schema.__name__}")
        return parsed

    @staticmethod
    def _record(purpose: str, model: str, usage) -> None:
        inp = usage.input_tokens or 0
        out = usage.output_tokens or 0
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
        cost = (inp * p_in + out * p_out + read * p_read + write * p_write) / 1_000_000
        store.record_usage(purpose, model, inp, out, read, write, cost)
